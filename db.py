"""SQLite database setup and query helpers."""

import json
import logging
import re
import sqlite3
import threading
from datetime import datetime, date, timedelta
from pathlib import Path

from config import settings

log = logging.getLogger(__name__)

# Connection model (2026-09-01 review, P1).
#
# One connection was shared by every thread with check_same_thread=False
# and no lock: the event loop, asyncio.to_thread(poll_and_download), the
# bridge jobs, the bridge-dump executor and the weekly VACUUM. SQLite's
# own mutex kept that from corrupting memory, but transactions are
# per-connection: thread B's commit() committed whatever thread A had
# half-written, and VACUUM could land inside another thread's implicit
# transaction. Latent (no error had surfaced) and growing with the
# analyst-log, chat-ingestion and ask-log write volume.
#
# Now: the MAIN thread keeps the module-level `_conn` (exact old
# behaviour, and the legacy `db._conn = None` reset that test scripts
# use still works); every other thread gets its own connection from a
# threading.local. WAL mode lets readers and one writer overlap, and
# busy_timeout absorbs the rare write-write collision. Schema and
# migrations run once, under a lock, on the first connection. Every
# DML helper in this module commits before returning (verified by AST
# scan on 2026-09-01), so no cross-thread reader ever depended on
# seeing another thread's uncommitted rows.
_conn: sqlite3.Connection | None = None
_local = threading.local()
_schema_lock = threading.Lock()
_schema_ready = False
_BUSY_TIMEOUT_S = 30


def _open_connection() -> sqlite3.Connection:
    db_path = Path(settings.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False,
                           timeout=_BUSY_TIMEOUT_S)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_S * 1000}")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    global _schema_ready
    with _schema_lock:
        if _schema_ready:
            return
        _init_schema(conn)
        _migrate_drop_unique_constraints(conn)
        _migrate_add_extraction_source(conn)
        _migrate_add_lean_prev_seen(conn)
        try:
            _migrate_pdf_query_surface(conn)
            _migrate_calendar_posts_lineup(conn)
        except Exception as e:  # never block boot on a query-surface migration
            log.warning(f"pdf query-surface migration skipped: {e}")
        _schema_ready = True


def get_connection() -> sqlite3.Connection:
    global _conn
    if threading.current_thread() is threading.main_thread():
        if _conn is None:
            _conn = _open_connection()
            _schema_ready_reset_if_new_path()
            _ensure_schema(_conn)
        return _conn
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _open_connection()
        _ensure_schema(conn)
        _local.conn = conn
    return conn


_schema_path: str | None = None


def _schema_ready_reset_if_new_path() -> None:
    """Test scripts repoint settings.db_path and set `db._conn = None`
    to get a fresh database; the schema must then be created again."""
    global _schema_ready, _schema_path
    if _schema_path != str(settings.db_path):
        _schema_path = str(settings.db_path)
        _schema_ready = False


def reset_connections() -> None:
    """Close and forget every connection this module holds (tests)."""
    global _conn, _schema_ready
    for c in (_conn, getattr(_local, "conn", None)):
        try:
            if c is not None:
                c.close()
        except Exception:
            pass
    _conn = None
    _local.conn = None
    _schema_ready = False


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS dropbox_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            cursor TEXT,
            last_poll_at TEXT
        );

        INSERT OR IGNORE INTO dropbox_state (id, cursor, last_poll_at) VALUES (1, NULL, NULL);

        CREATE TABLE IF NOT EXISTS pdf_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dropbox_path TEXT NOT NULL UNIQUE,
            dropbox_rev TEXT,
            file_name TEXT NOT NULL,
            file_size_bytes INTEGER,
            dropbox_modified_at TEXT,
            downloaded_at TEXT,
            local_path TEXT,
            status TEXT NOT NULL DEFAULT 'DOWNLOADED',
            priority TEXT,
            error_message TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_pdf_files_status ON pdf_files(status);
        CREATE INDEX IF NOT EXISTS idx_pdf_files_downloaded_at ON pdf_files(downloaded_at);

        -- NOTE: no UNIQUE on pdf_file_id — we keep every analysis run as history.
        -- Use ORDER BY id DESC LIMIT 1 to get the latest for a pdf_file_id.
        CREATE TABLE IF NOT EXISTS pdf_analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pdf_file_id INTEGER NOT NULL REFERENCES pdf_files(id),
            triage_json TEXT,
            analysis_json TEXT,
            priority TEXT,
            pages_analyzed INTEGER,
            total_pages INTEGER,
            input_tokens_used INTEGER,
            output_tokens_used INTEGER,
            model_used TEXT,
            analysis_duration_seconds REAL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_pdf_analyses_file_id ON pdf_analyses(pdf_file_id);

        -- NOTE: no UNIQUE on (report_date, report_type) — every pulse run kept as history.
        CREATE TABLE IF NOT EXISTS daily_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_date TEXT NOT NULL,
            report_type TEXT NOT NULL,
            report_json TEXT NOT NULL,
            report_markdown TEXT NOT NULL,
            pdf_count INTEGER,
            input_tokens_used INTEGER,
            output_tokens_used INTEGER,
            discord_sent_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_daily_reports_type_created ON daily_reports(report_type, created_at);

        CREATE TABLE IF NOT EXISTS processing_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pdf_file_id INTEGER REFERENCES pdf_files(id),
            event_type TEXT NOT NULL,
            status TEXT NOT NULL,
            details TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        -- Single-row state for the real-time ingestion-feed Discord poster.
        -- Tracks the highest pdf_file_id we've already announced so we don't
        -- repost. id = 1 always (the row is upserted in place).
        CREATE TABLE IF NOT EXISTS ingest_feed_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            last_announced_pdf_file_id INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        INSERT OR IGNORE INTO ingest_feed_state (id, last_announced_pdf_file_id) VALUES (1, 0);

        -- Per-PDF state machine for the parallel Opus routine ingestion bridge.
        -- Only HIGH-priority PDFs land here when settings.high_ingestion_backend
        -- is set to "opus_bridge". Status values:
        --   pending             — queued, not yet committed to the bridge branch
        --   committed           — PDF + sidecar pushed to ingest-pending/ on the bridge
        --   completed           — Opus routine finished; result pulled into pdf_analyses
        --   fallback_to_gemini  — bridge stalled / failed / oversized → Gemini ran instead
        --   failed              — both bridge and Gemini fallback failed
        CREATE TABLE IF NOT EXISTS bridge_ingestion_state (
            pdf_file_id INTEGER PRIMARY KEY,
            status TEXT NOT NULL,
            queued_at TEXT NOT NULL DEFAULT (datetime('now')),
            committed_at TEXT,
            completed_at TEXT,
            bridge_filename TEXT,
            bridge_commit_sha TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            error_message TEXT,
            fallback_reason TEXT,
            FOREIGN KEY (pdf_file_id) REFERENCES pdf_files(id)
        );
        CREATE INDEX IF NOT EXISTS idx_bridge_status ON bridge_ingestion_state(status);
        CREATE INDEX IF NOT EXISTS idx_bridge_queued_at ON bridge_ingestion_state(queued_at);

        -- Persistent /reanalyze job state. The Discord interaction handler
        -- previously ran reanalyze synchronously, which capped at Discord's
        -- 15-min interaction limit and lost all state on Railway redeploy.
        -- This table moves the job to a scheduler-driven background path
        -- with DB-persisted progress, so a worker restart resumes where
        -- it left off and large reanalyzes run to completion regardless
        -- of Discord interaction expiry.
        CREATE TABLE IF NOT EXISTS reanalyze_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            status TEXT NOT NULL,  -- pending | processing | complete | failed | cancelled
            hours INTEGER NOT NULL,
            priority_filter TEXT,  -- JSON array of priority strings, or NULL for all
            target_pdf_ids TEXT NOT NULL,  -- JSON array of pdf_files.id
            target_count INTEGER NOT NULL,
            processed_pdf_ids TEXT NOT NULL DEFAULT '[]',  -- JSON
            failed_pdf_ids TEXT NOT NULL DEFAULT '[]',  -- JSON
            bridge_queued_pdf_ids TEXT NOT NULL DEFAULT '[]',  -- JSON
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            requested_by TEXT,           -- Discord user id who triggered
            discord_channel_id INTEGER,  -- Channel where /reanalyze was issued
            discord_status_message_id INTEGER,  -- Message to update with progress
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            started_at TEXT,
            completed_at TEXT,
            last_progress_at TEXT,
            error_message TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_reanalyze_status ON reanalyze_jobs(status);
        CREATE INDEX IF NOT EXISTS idx_reanalyze_created ON reanalyze_jobs(created_at);

        -- Per-user audit log for /ask + @mention Perplexity queries. Used to
        -- enforce a daily-per-user cap. Reset is "since UTC midnight" — see
        -- count_ask_queries_today_for_user.
        CREATE TABLE IF NOT EXISTS ask_queries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            asked_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_ask_queries_user_time ON ask_queries(user_id, asked_at);

        -- Cross-window anti-recycling state. Records every /ask reply the
        -- bot sends so the next /ask call from the same asker (in the same
        -- channel) can see what the bot already told them — even when the
        -- prior reply has scrolled past the 50-msg / 24h chat_context window
        -- (fast-moving channels like stonks-yapping eat the [YOU said
        -- earlier]: trace in under 30 min). Without this the anti-recycling
        -- rule has nothing to act on and the bot reuses the same hook
        -- (e.g. "LARPing as a quant") across exchanges.
        CREATE TABLE IF NOT EXISTS ask_bot_answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asker_user_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            question TEXT,
            answer TEXT NOT NULL,
            answered_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_ask_bot_answers_asker_channel_ts
            ON ask_bot_answers(asker_user_id, channel_id, answered_at DESC);

        -- Company logos for the calendar's earnings rows (2026-08-26).
        -- Stores the RENDER-READY PNG bytes, already downscaled to the
        -- row height, not the original artwork: the renderer must never
        -- touch the network or resize, and a row logo is ~2KB at that
        -- size.
        --
        -- A symbol Finnhub has no logo for is cached as a ZERO-BYTE
        -- blob. That is a real answer ("asked, none exists"), and
        -- caching it is what stops a nightly re-fetch of every logoless
        -- name. get_symbol_logos gives those the short TTL, same split
        -- as the market caps.
        -- Calendar sheet messages the bot posted, so the morning refresh
        -- can edit them in place instead of posting again (2026-09-01).
        CREATE TABLE IF NOT EXISTS calendar_posts (
            date_iso TEXT NOT NULL,
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            lineup_hash TEXT NOT NULL,
            lineup_json TEXT,
            posted_at TEXT NOT NULL DEFAULT (datetime('now')),
            refreshed_at TEXT,
            PRIMARY KEY (date_iso, channel_id)
        );

        CREATE TABLE IF NOT EXISTS symbol_logo (
            symbol TEXT PRIMARY KEY,
            image BLOB,
            fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        -- Books the bot published, so a caller's correction can be
        -- attributed (2026-08-26). BK's book listed MU 980C and AVGO
        -- 450C as open; he replied "No AVGO" / "No MU anymore". Both
        -- were true -- he had exited and never posted an exit in his
        -- alerts channel, so analyst_trades held an open with no close
        -- and the book had no way to know.
        --
        -- The correction is only interpretable against the book it
        -- corrects: "No AVGO" alone is not a trade event, it is a
        -- pronoun. This table is the antecedent. Retained briefly --
        -- a correction lands within minutes or not at all.
        CREATE TABLE IF NOT EXISTS bot_book_posts (
            discord_message_id INTEGER PRIMARY KEY,
            channel_id INTEGER NOT NULL,
            caller TEXT NOT NULL,       -- whose book (canonical lowercase)
            tickers TEXT NOT NULL,      -- JSON list, as published
            posted_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_bot_book_posts_lookup
            ON bot_book_posts(channel_id, caller, posted_at DESC);

        -- Format-overhaul Phase 1 state (2026-06-10). Two tables:
        --
        -- pulse_state: compact per-context-dump snapshot of the theme map
        -- + high-conviction calls. The dump job inserts a candidate row on
        -- every context dump; when the bridge posts a real daily pulse it
        -- stamps the consumed candidate with the pulse date. The WHAT
        -- CHANGED section is computed by diffing today's stamped state
        -- against the previous stamped state.
        CREATE TABLE IF NOT EXISTS pulse_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dumped_at TEXT NOT NULL,
            pulse_date TEXT,          -- NULL until a daily pulse consumes it
            state_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_pulse_state_dumped ON pulse_state(dumped_at DESC);
        CREATE INDEX IF NOT EXISTS idx_pulse_state_pulse_date ON pulse_state(pulse_date);

        -- pulse_leans: every trade lean the daily pulse ships, tracked
        -- across days so the TRADE BOARD can show NEW vs LIVE (dN) and
        -- age stale leans out. Leans are extracted deterministically from
        -- the final pulse markdown's INSIGHTS closing paragraphs.
        CREATE TABLE IF NOT EXISTS pulse_leans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            instrument TEXT NOT NULL,        -- ticker without $
            direction TEXT NOT NULL,         -- 'long' | 'short'
            first_seen_date TEXT NOT NULL,   -- YYYY-MM-DD
            last_seen_date TEXT NOT NULL,    -- YYYY-MM-DD (updated on re-appearance)
            -- The last_seen_date from BEFORE the most recent upsert. Makes
            -- a gap visible: a lean dropped and re-added keeps its original
            -- first_seen_date and gets last_seen stamped to today, so
            -- without this the board cannot tell a continuous hold from a
            -- re-entry (2026-08-12, "held since Aug 7" for a $MU that was
            -- off the board for two sessions). NULL on rows written before
            -- the column existed and on a lean's first sighting.
            prev_seen_date TEXT,
            context_snippet TEXT,            -- the lean sentence, clipped
            status TEXT NOT NULL DEFAULT 'live',  -- 'live' | 'aged_out'
            UNIQUE(instrument, direction, first_seen_date)
        );
        CREATE INDEX IF NOT EXISTS idx_pulse_leans_status ON pulse_leans(status, last_seen_date DESC);

        -- reminder_sent: dedup guard for the channel reminder system.
        -- One row per (fire_date, event_id, lead) actually posted, so a
        -- mid-day redeploy can't double-post a reminder. The event
        -- calendar itself lives in reminders/calendar.json (Claude-edited,
        -- version-controlled) — this table only records what already fired.
        CREATE TABLE IF NOT EXISTS reminder_sent (
            fire_date TEXT NOT NULL,   -- YYYY-MM-DD the reminder posted
            event_id  TEXT NOT NULL,   -- calendar entry id
            lead      INTEGER NOT NULL,
            sent_at   TEXT NOT NULL,
            UNIQUE(fire_date, event_id, lead)
        );

        -- Analyst trade log. One row per image attachment posted in the
        -- analyst alerts channel. Populated by analyst_log.watcher via
        -- Gemini vision. is_trade=0 rows are non-trade images (memes,
        -- tweets) — recorded for dedup so we don't re-OCR them.
        -- See analyst_log/ocr.py for the field semantics.
        CREATE TABLE IF NOT EXISTS analyst_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_message_id INTEGER NOT NULL,
            discord_attachment_id INTEGER NOT NULL,
            author TEXT NOT NULL,
            author_id INTEGER,        -- Discord user ID (NULL on legacy rows)
            posted_at TEXT NOT NULL,
            image_url TEXT,
            caption TEXT,
            is_trade INTEGER NOT NULL DEFAULT 0,
            ticker TEXT,
            contract_type TEXT,
            strike REAL,
            expiry TEXT,           -- YYYY-MM-DD
            action TEXT,           -- open | add | trim | close | viewing | unclear
            gain_pct REAL,
            price REAL,            -- entry/exit price (midpoint of bid/ask when stats screen)
            inferred_status TEXT,  -- e.g. 'expired_unknown' (set by daily cron)
            tracking_mode TEXT NOT NULL DEFAULT 'caller',
                                   -- 'caller' = official analyst_callers entry (gets announce
                                   --   embed + W/L tracker + RECENT TRADES surface in /ask)
                                   -- 'member' = any user posting in an eager-OCR alert
                                   --   channel; row gets persisted for future scoring/data,
                                   --   no announce, never bleeds into caller /ask context
            extraction_source TEXT,
                                   -- 'image' = image-OCR pipeline (original path)
                                   -- 'text'  = text classifier (2026-06-02 — no attachments)
                                   -- 'mixed' = classifier saw both text + image evidence
                                   -- Legacy rows (pre-2026-06-02) backfilled to 'image'.
            gemini_json TEXT,      -- raw OCR JSON for forensics
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(discord_message_id, discord_attachment_id)
        );
        CREATE INDEX IF NOT EXISTS idx_analyst_trades_posted ON analyst_trades(posted_at);
        CREATE INDEX IF NOT EXISTS idx_analyst_trades_expiry ON analyst_trades(expiry);
        CREATE INDEX IF NOT EXISTS idx_analyst_trades_ticker ON analyst_trades(ticker);

        -- analyst_trades is 98% NON-trades. The classifier writes a row
        -- for every message it inspects, and 75,960 of 77,362 rows carry
        -- is_trade=0 with ticker/action/gain_pct all NULL. Any query that
        -- forgets `WHERE is_trade = 1` reads ~2% signal.
        --
        -- The view exists so the read path cannot forget. query_data's
        -- tool docs point at it, which is enforcement rather than a
        -- reminder -- the same reason the meta-plumbing rule moved out of
        -- the prompt and into code.
        CREATE VIEW IF NOT EXISTS analyst_trades_real AS
            SELECT * FROM analyst_trades WHERE is_trade = 1;
        CREATE INDEX IF NOT EXISTS idx_analyst_trades_is_trade
            ON analyst_trades(is_trade);
        -- Indexes on tracking_mode / author_id moved OUT of executescript().
        -- On a live deploy with the legacy schema, those columns don't
        -- exist yet at this point — they get added by the ALTER TABLE
        -- migrations a few lines down. Index creation now lives there too
        -- so the column is guaranteed present before the CREATE INDEX runs.

        -- LLM-generated personality profiles for active group members.
        -- Populated by scripts/backfill_user_profiles.py (initial) and a
        -- weekly refresh job (rolling). Used by /ask to inject a
        -- "WHO'S TALKING" block when responding in any channel where
        -- profiled users have posted recently.
        --
        -- message_count_at_update is the baseline; refresh logic uses
        -- (current_count - baseline > 20) to decide whether to re-profile.
        -- user_profiles now carries BOTH the prose dossier AND the two
        -- hidden hierarchy metrics. Single backfill pass refreshes
        -- everything; single context block surfaces it together.
        --
        -- slur_count: raw count of regex-matched slur occurrences in
        -- the user's own messages over the most-recent backfill window.
        -- Patterns live in scripts/slur_patterns.py. Noisy by design.
        --
        -- racial_humor_score: 0-100 LLM-assessed score for broader racial
        -- humor / ethnic jokes / stereotyping (the soft stuff regex
        -- misses). Designed to INCLUDE literal slur usage too, so it's
        -- a complete picture — slur_count is a regex-based floor signal,
        -- racial_humor_score is the composite.
        --
        -- trader_score: 0-100 LLM-assessed skill score.
        -- trader_rank: DEPRECATED stored column. Now computed on-read
        -- via get_global_trader_ranks() so it's always fresh. Old
        -- values may still linger in this column from past deploys;
        -- ignore them — the upsert path no longer touches this column
        -- and the live computation is the source of truth.
        -- trader_rationale: one-line LLM justification for the score.
        CREATE TABLE IF NOT EXISTS user_profiles (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            display_name TEXT,
            profile_text TEXT NOT NULL,
            message_count_at_update INTEGER NOT NULL DEFAULT 0,
            last_seen_message_at TEXT,
            slur_count INTEGER NOT NULL DEFAULT 0,
            racial_humor_score INTEGER,
            trader_score INTEGER,
            trader_rank INTEGER,
            trader_rationale TEXT,
            -- racism_rationale: 1-2 savage-but-hilarious sentences
            -- distilling WHY the user ranks where they do on racism.
            -- LLM-generated during profile refresh; sourced from
            -- specific chat content (the Retarded takes / Voice /
            -- Recent personal life sections). Surfaced alongside
            -- racism-rank in /ask answers. Same shape as
            -- trader_rationale but for the racism axis.
            racism_rationale TEXT,
            slur_examples TEXT,
            trader_examples TEXT,
            -- personal_ammo: JSON list[str] of broader weaponizable
            -- snippets — slurs AND dumb takes AND embarrassing claims
            -- AND boasts that aged badly AND math fails AND broken-
            -- English moments. LLM-extracted (not regex), so it
            -- captures content the deterministic slur_examples regex
            -- can't. Surfaced in /ask dossier as "recent ammo".
            personal_ammo TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_user_profiles_username ON user_profiles(username);

        -- Persistent chat-message store. Every message in a configured
        -- ingestion channel gets a row here. Two use cases:
        --
        --   1. Verbatim claim-verification — when the bot is challenged
        --      with "show me where I said that", we can SQL-grep the
        --      user's actual messages and quote the exact line.
        --
        --   2. Source-of-truth for the profile-refresh pipeline. Instead
        --      of scanning Discord history on every refresh (rate-limited,
        --      WS-flap-prone), the pipeline reads from this table. Daily
        --      refresh becomes a local SQL query.
        --
        -- discord_message_id is UNIQUE so re-ingestion (catch-up after a
        -- gateway flap) is idempotent — INSERT OR IGNORE silently skips
        -- rows we already have.
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_message_id INTEGER NOT NULL UNIQUE,
            channel_id INTEGER NOT NULL,
            channel_name TEXT NOT NULL,
            author_id INTEGER NOT NULL,
            author_username TEXT,
            author_display TEXT,
            content TEXT,
            posted_at TEXT NOT NULL,
            has_attachments INTEGER NOT NULL DEFAULT 0,
            attachment_urls TEXT,    -- JSON list[str]
            embed_texts TEXT,        -- JSON list[str]
            reply_parent_id INTEGER,
            -- image_ocr_text: lazy-populated text extracted from image
            -- attachments via Gemini vision. NULL until an OCR pass runs.
            -- Two write paths:
            --   - Lazy: /ask context-builder OCRs on demand when an
            --     attached image is about to be shown to Gemini.
            --   - Eager: ingest_message OCRs immediately for channels
            --     in chat_eager_ocr_channels (e.g. gain-loss-porn).
            -- Once non-NULL, never re-OCR'd (cache). Use `force=True`
            -- in the OCR helper to bust the cache for a specific row.
            image_ocr_text TEXT,
            image_ocr_status TEXT,   -- 'success', 'no_images', 'failed', NULL
            image_ocr_at TEXT,       -- ISO timestamp when OCR completed
            ingested_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_chat_messages_channel_ts ON chat_messages(channel_id, posted_at DESC);
        CREATE INDEX IF NOT EXISTS idx_chat_messages_author_ts  ON chat_messages(author_id, posted_at DESC);
        CREATE INDEX IF NOT EXISTS idx_chat_messages_username_ts ON chat_messages(author_username, posted_at DESC);

        -- 2026-08-10 latency review. Two index gaps, both found by running
        -- EXPLAIN QUERY PLAN over every SQL literal in this file against
        -- the production database (189,864 chat_messages).
        --
        -- 1. Every username lookup here is written LOWER(author_username)
        --    = LOWER(?), and a function on the column makes
        --    idx_chat_messages_username_ts unusable — SQLite fell back to
        --    a full scan plus a temp B-tree for ORDER BY. Five call sites:
        --    resolve_username_to_user_id, get_recent_user_messages,
        --    find_user_messages_matching, search_chat_messages_for_ask,
        --    and the pending-protected lookup at boot. Measured 0.035-0.052s
        --    each, all on the Discord event loop. An index ON THE
        --    EXPRESSION is what SQLite can match: 0.052s -> 0.000s.
        --    Keep the call sites written as LOWER(col) = ? — rewriting
        --    them to bare equality would silently stop matching this index.
        -- 2. Time-window retrieval filters bare posted_at, which is only
        --    ever a trailing column in the composite indexes above, so it
        --    could not be used as a range scan. 0.035s -> 0.000s for a
        --    200-row window, 0.048s -> 0.015s for the profile bulk read.
        --
        -- Both built in under 0.5s against the live 205MB database.
        CREATE INDEX IF NOT EXISTS idx_chat_messages_lower_username ON chat_messages(LOWER(author_username), posted_at DESC);
        CREATE INDEX IF NOT EXISTS idx_chat_messages_posted_at ON chat_messages(posted_at);

        -- Protected members promoted from PROTECTED_PENDING_USERNAMES:
        -- a member who hasn't joined yet is registered by exact username;
        -- the first ingested message from that username pins the
        -- permanent author_id here. One row per username (first sighting
        -- wins) so a later re-claim of a released username cannot
        -- inherit protection. Runtime protected set = env-var IDs union
        -- this table.
        -- Symbol market-cap cache (2026-08-20, daily calendar graphic).
        -- Finnhub /stock/profile2 lookups are paced at ~1/s under the
        -- free 60/min limit; a 7-day TTL keeps the nightly refresh to
        -- the handful of symbols not seen this week. cap in $M as
        -- Finnhub returns it; name rides along for the render.
        CREATE TABLE IF NOT EXISTS symbol_market_cap (
            symbol TEXT PRIMARY KEY,
            market_cap_musd REAL,
            name TEXT,
            fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        -- Chat-catchup watermark (2026-08-23). The gap detector treats
        -- ANY 60-min silence in the last 30 days as a gap, so every
        -- channel permanently "has a gap" (overnight) and every boot /
        -- resume / 4h tick re-walked a month of all 15 channels storing
        -- 0 rows (26 runs on 2026-08-23 alone). Once a range has been
        -- fully rescanned it is SEALED here; gap detection only looks
        -- past scanned_through.
        CREATE TABLE IF NOT EXISTS chat_catchup_watermark (
            channel_id INTEGER PRIMARY KEY,
            scanned_through TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        -- Sleeper player-ID -> name cache (2026-08-20). The fantasy
        -- /ask tool needs id->name translation for rosters/matchups/
        -- transactions; Sleeper's full players dump is ~15MB so it is
        -- fetched at most daily (scheduler job + lazy first-use) and
        -- trimmed to these four fields (~11K rows, <1MB).
        CREATE TABLE IF NOT EXISTS sleeper_players (
            player_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            position TEXT,
            team TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS protected_users (
            author_id   INTEGER PRIMARY KEY,
            username    TEXT NOT NULL UNIQUE,
            promoted_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        -- Pipeline-event audit trail (fix #7 — observability). Generic
        -- across pipeline jobs: profile refresh, chat catchup, anything
        -- else worth tracking historically. Distinct from processing_log
        -- which is PDF-pipeline-specific (has pdf_file_id FK). Payload
        -- is free-form JSON; consumers query by event_type + created_at.
        CREATE TABLE IF NOT EXISTS pipeline_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,    -- 'profile_refresh', 'chat_catchup', etc.
            status TEXT NOT NULL,         -- 'completed', 'failed', 'partial'
            payload TEXT,                 -- JSON dump of run stats
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_pipeline_events_type_ts ON pipeline_events(event_type, created_at DESC);
    """)
    # Migration: add the metrics columns to user_profiles if a previous
    # deploy used the old schema. Must run BEFORE the slur_count /
    # trader_rank / racial_humor indexes are created, since the indexes
    # reference these new columns.
    for col, ddl in [
        ("slur_count", "ALTER TABLE user_profiles ADD COLUMN slur_count INTEGER NOT NULL DEFAULT 0"),
        ("racial_humor_score", "ALTER TABLE user_profiles ADD COLUMN racial_humor_score INTEGER"),
        ("trader_score", "ALTER TABLE user_profiles ADD COLUMN trader_score INTEGER"),
        ("trader_rank", "ALTER TABLE user_profiles ADD COLUMN trader_rank INTEGER"),
        ("trader_rationale", "ALTER TABLE user_profiles ADD COLUMN trader_rationale TEXT"),
        ("racism_rationale", "ALTER TABLE user_profiles ADD COLUMN racism_rationale TEXT"),
        ("slur_examples", "ALTER TABLE user_profiles ADD COLUMN slur_examples TEXT"),
        ("trader_examples", "ALTER TABLE user_profiles ADD COLUMN trader_examples TEXT"),
        ("personal_ammo", "ALTER TABLE user_profiles ADD COLUMN personal_ammo TEXT"),
        # Deep-rebuild tracking (2026-07-11): incremental refreshes are a
        # one-way valve — anything a window missed is unrecoverable. Each
        # profile now records its last COLD-START rebuild so the refresh
        # job can cycle every profile through a 90-day from-scratch pass
        # (ZHawk's 46 fitness messages never survived the valve).
        ("last_full_rebuild_at", "ALTER TABLE user_profiles ADD COLUMN last_full_rebuild_at TEXT"),
        # analyst_trades migration: add price + caller columns on existing deploys.
        ("price", "ALTER TABLE analyst_trades ADD COLUMN price REAL"),
        ("caller", "ALTER TABLE analyst_trades ADD COLUMN caller TEXT"),
        # Universal trade tracking — tracking_mode separates official-caller
        # rows from member-posted alerts in shared/eager-OCR channels.
        # author_id is the Discord user ID (NULL on legacy rows where only
        # the display name was stored). Both default-safe for old rows.
        ("tracking_mode", "ALTER TABLE analyst_trades ADD COLUMN tracking_mode TEXT NOT NULL DEFAULT 'caller'"),
        ("author_id", "ALTER TABLE analyst_trades ADD COLUMN author_id INTEGER"),
        # chat_messages OCR columns — added when image OCR landed. Live
        # deploys with the older 13-column chat_messages table get these
        # three columns appended (SQLite ALTER TABLE ADD COLUMN is cheap;
        # no data rewrite, just a metadata change).
        ("image_ocr_text", "ALTER TABLE chat_messages ADD COLUMN image_ocr_text TEXT"),
        ("image_ocr_status", "ALTER TABLE chat_messages ADD COLUMN image_ocr_status TEXT"),
        ("image_ocr_at", "ALTER TABLE chat_messages ADD COLUMN image_ocr_at TEXT"),
    ]:
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass  # duplicate column — already migrated
    # Backfill caller='abe' for pre-existing CALLER rows (single-caller
    # era). 2026-07-11 FIX: the original version had no tracking_mode
    # filter, so it stamped every member-mode row (caller=NULL by
    # design) as abe's on every boot — 432 rows from 39 different
    # members were mislabeled, which silently shrank the outcome guard's
    # member-name protection set to three names. Scoped + repaired.
    try:
        conn.execute(
            "UPDATE analyst_trades SET caller = 'abe' "
            "WHERE caller IS NULL AND tracking_mode = 'caller'"
        )
        conn.execute(
            "UPDATE analyst_trades SET caller = NULL "
            "WHERE tracking_mode = 'member' AND caller IS NOT NULL"
        )
    except sqlite3.OperationalError:
        pass
    # Self-heal for placeholder-corrupted profiles (2026-07-11): the
    # first deep-rebuild batch shipped 4 dossiers whose personal-life
    # section was the spec's literal '[bracket] + [bracket]' shape
    # template. The '] + [' sequence only occurs in that placeholder
    # shape (real bullets are 'text + [framing]' — one bracket group).
    # Resetting last_full_rebuild_at puts them back at the FRONT of the
    # rebuild queue, so the fixed prompt + lint redo them on the next
    # tick. Idempotent: clean profiles never match.
    try:
        conn.execute(
            "UPDATE user_profiles SET last_full_rebuild_at = NULL "
            "WHERE profile_text LIKE '%] + [%'"
        )
    except sqlite3.OperationalError:
        pass
    # Fiction-bleed self-heal (2026-07-11 19:00 batch): the model lifted
    # "the yard" from the spec's FICTIONAL shape example into 2pale's
    # real dossier (verified: he has never said it). Profiles carrying
    # any fictional-example signature get re-queued for rebuild under
    # the fiction-token hard-lint. Worst case on a false positive (a
    # user who really says "the yard") is one extra rebuild.
    try:
        conn.execute(
            "UPDATE user_profiles SET last_full_rebuild_at = NULL "
            "WHERE profile_text LIKE '%the yard%' "
            "   OR profile_text LIKE '%microwave% fish%' "
            "   OR profile_text LIKE '%the options incident%'"
        )
    except sqlite3.OperationalError:
        pass
    # Exit-linking backfill (2026-07-11): historical strikeless closes
    # inherit contract fields from their scope's unclosed entries, so
    # the position rollup finally sees open→close in one partition
    # (outcome coverage was 40/447 member rows). Idempotent — a filled
    # row has a strike and is skipped on the next boot. Runs after the
    # member-caller repair above so scope matching sees clean data.
    try:
        _n_exit_links = backfill_orphan_exit_links()
        if _n_exit_links:
            import logging as _logging
            _logging.getLogger(__name__).info(
                f"migration: linked {_n_exit_links} orphan exit(s) to "
                f"their entries"
            )
    except Exception:
        pass
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_analyst_trades_caller ON analyst_trades(caller)"
    )
    # Indexes for tracking_mode + author_id (cheap on small table; speeds up
    # both the caller-only context filters and member-mode score lookups).
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_analyst_trades_tracking ON analyst_trades(tracking_mode)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_analyst_trades_author_id ON analyst_trades(author_id)"
    )
    # Backfill author_id on legacy caller rows. The column was added in the
    # universal-tracking migration (Commit 4b0a7b1); rows written before
    # that have caller='bankerkyle' / 'abe' / etc. but NULL author_id.
    # The new 1/3/5 points-ledger SQL keys on author_id, so without this
    # backfill the new scoring layer is silently inert for the two most
    # well-documented users in the room (BK has 44 trade rows, Abe has 51,
    # all author_id=NULL until this migration runs).
    #
    # Lookup: caller name → user_id by joining chat_messages on the caller's
    # configured username. Idempotent — WHERE author_id IS NULL ensures
    # the UPDATE never overwrites valid rows.
    try:
        from config import settings as _settings  # local import to avoid cycle at module top
        for c in _settings.resolve_analyst_callers():
            uname = (c.get("username") or "").strip().lower()
            caller_name = (c.get("name") or "").strip().lower()
            if not uname or not caller_name:
                continue
            row = conn.execute(
                "SELECT author_id FROM chat_messages "
                "WHERE LOWER(author_username) = ? AND author_id IS NOT NULL "
                "LIMIT 1",
                (uname,),
            ).fetchone()
            if not row or not row[0]:
                continue
            uid = int(row[0])
            conn.execute(
                "UPDATE analyst_trades SET author_id = ? "
                "WHERE LOWER(caller) = ? AND author_id IS NULL",
                (uid, caller_name),
            )
    except Exception:
        # Settings import or chat_messages query failed — don't block boot.
        # Backfill will re-attempt next boot or can be run manually.
        pass
    # Now-safe indexes that depend on the migrated columns.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_profiles_slur ON user_profiles(slur_count DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_profiles_racial_humor ON user_profiles(racial_humor_score DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_profiles_rank ON user_profiles(trader_rank)")
    # Idempotent migrations for already-deployed bridge_ingestion_state schemas
    # (the table was first created in step 1 without these columns).
    for col, ddl in [
        ("bridge_commit_sha", "ALTER TABLE bridge_ingestion_state ADD COLUMN bridge_commit_sha TEXT"),
        ("attempt_count", "ALTER TABLE bridge_ingestion_state ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0"),
    ]:
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError as e:
            # "duplicate column name" = already migrated, ignore
            if "duplicate column" not in str(e).lower():
                raise
    conn.commit()


def _migrate_drop_unique_constraints(conn: sqlite3.Connection) -> None:
    """One-time migration: drop UNIQUE(pdf_file_id) and UNIQUE(report_date, report_type).

    SQLite can't DROP an implicit unique index defined in CREATE TABLE. The only way
    is to rebuild the table. Safe because data is preserved, structure is relaxed.
    """
    import logging
    log = logging.getLogger(__name__)

    def has_unique(table: str, col_tuple: tuple[str, ...]) -> bool:
        """Check if a table has a UNIQUE index covering exactly these columns."""
        rows = conn.execute(f"PRAGMA index_list('{table}')").fetchall()
        for r in rows:
            if r["unique"] and r["origin"] == "u":  # 'u' means explicit or auto UNIQUE
                idx_name = r["name"]
                cols = [c["name"] for c in conn.execute(f"PRAGMA index_info('{idx_name}')").fetchall()]
                if tuple(cols) == col_tuple:
                    return True
        return False

    # pdf_analyses: drop UNIQUE(pdf_file_id)
    if has_unique("pdf_analyses", ("pdf_file_id",)):
        log.info("Migrating pdf_analyses: dropping UNIQUE(pdf_file_id) to preserve analysis history")
        conn.executescript("""
            CREATE TABLE pdf_analyses_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pdf_file_id INTEGER NOT NULL REFERENCES pdf_files(id),
                triage_json TEXT,
                analysis_json TEXT,
                priority TEXT,
                pages_analyzed INTEGER,
                total_pages INTEGER,
                input_tokens_used INTEGER,
                output_tokens_used INTEGER,
                model_used TEXT,
                analysis_duration_seconds REAL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            INSERT INTO pdf_analyses_new
                SELECT id, pdf_file_id, triage_json, analysis_json, priority,
                       pages_analyzed, total_pages, input_tokens_used, output_tokens_used,
                       model_used, analysis_duration_seconds, created_at
                FROM pdf_analyses;
            DROP TABLE pdf_analyses;
            ALTER TABLE pdf_analyses_new RENAME TO pdf_analyses;
            CREATE INDEX IF NOT EXISTS idx_pdf_analyses_file_id ON pdf_analyses(pdf_file_id);
        """)
        conn.commit()

    # daily_reports: drop UNIQUE(report_date, report_type)
    if has_unique("daily_reports", ("report_date", "report_type")):
        log.info("Migrating daily_reports: dropping UNIQUE(report_date, report_type) to preserve pulse history")
        conn.executescript("""
            CREATE TABLE daily_reports_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_date TEXT NOT NULL,
                report_type TEXT NOT NULL,
                report_json TEXT NOT NULL,
                report_markdown TEXT NOT NULL,
                pdf_count INTEGER,
                input_tokens_used INTEGER,
                output_tokens_used INTEGER,
                discord_sent_at TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            INSERT INTO daily_reports_new
                SELECT id, report_date, report_type, report_json, report_markdown,
                       pdf_count, input_tokens_used, output_tokens_used,
                       discord_sent_at, created_at
                FROM daily_reports;
            DROP TABLE daily_reports;
            ALTER TABLE daily_reports_new RENAME TO daily_reports;
            CREATE INDEX IF NOT EXISTS idx_daily_reports_type_created ON daily_reports(report_type, created_at);
        """)
        conn.commit()


def _migrate_add_lean_prev_seen(conn: sqlite3.Connection) -> None:
    """Add prev_seen_date to pulse_leans (2026-08-12).

    The TRADE BOARD labelled a lean "held since <first_seen_date>" for
    anything whose first_seen_date was not today. That cannot distinguish
    a continuous hold from a re-entry: a lean dropped and re-added keeps
    its original first_seen_date, and the upsert stamps last_seen_date to
    today, so the gap is erased.

    Both cases shipped on 2026-08-12. $MU rendered "held since Aug 7"
    having been off the board on Aug 10 and Aug 11. $QQQ puts rendered
    "held since Aug 10" having been off the board on Aug 11, where it was
    scored "flat (-0.3%) since flagged" on its way out. A follower reads
    an unbroken two-day call in both cases.

    prev_seen_date holds the last_seen_date from BEFORE today's upsert,
    which is what makes the gap visible. Idempotent: PRAGMA-checks before
    ALTER. Existing rows get NULL, which the renderer treats as unknown
    and falls back to the old behaviour rather than inventing a re-entry.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pulse_leans)")}
    if "prev_seen_date" not in cols:
        conn.execute("ALTER TABLE pulse_leans ADD COLUMN prev_seen_date TEXT")
        conn.commit()


def _migrate_add_extraction_source(conn: sqlite3.Connection) -> None:
    """Add extraction_source column to analyst_trades (2026-06-02).

    Tracks which modality produced each row:
      - 'image' : image-OCR pipeline (the original path)
      - 'text'  : text classifier (no image attachments)
      - 'mixed' : classifier consumed both text + image evidence

    Idempotent: PRAGMA-checks for the column before ALTER, then derives
    the label from image_url. Safe to run on every connection boot.

    2026-07-30 — this used to backfill NULL -> 'image' unconditionally,
    on the theory that it was a one-time pass over legacy rows. It is
    not one-time: it runs on every boot, and record_analyst_trade never
    wrote the column, so EVERY row it inserted was NULL and got stamped
    'image' at the next restart. 31,356 July chat messages were labelled
    screenshots, and the column claimed text extraction had been dead
    since 2026-06-01 while it was actually pulling 369 trades a month.

    image_url is the ground truth: a row with no image cannot have come
    from the image-OCR pipeline. Explicit 'text'/'mixed' labels are left
    alone — the classifier sets those deliberately.
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(analyst_trades)").fetchall()]
    if "extraction_source" not in cols:
        conn.execute("ALTER TABLE analyst_trades ADD COLUMN extraction_source TEXT")
    # Image evidence = a stored url OR an attachment id. 3 prod rows
    # carry an attachment id with no url (upload presumably failed to
    # record); treat those as image-sourced rather than guessing 'text'.
    _no_image = ("image_url IS NULL "
                 "AND COALESCE(discord_attachment_id, 0) = 0")
    # Label unlabelled rows from the evidence that produced them.
    conn.execute(
        "UPDATE analyst_trades "
        f"   SET extraction_source = CASE WHEN {_no_image} "
        "                            THEN 'text' ELSE 'image' END "
        " WHERE extraction_source IS NULL"
    )
    # Repair rows the old blanket backfill already mislabelled. Only
    # touches 'image' rows with no image evidence — idempotent after.
    conn.execute(
        "UPDATE analyst_trades SET extraction_source = 'text' "
        f" WHERE extraction_source = 'image' AND {_no_image}"
    )
    conn.commit()


# --- Dropbox state ---



# --- PDF files ---



# --- Analyses ---



def _migrate_calendar_posts_lineup(conn) -> None:
    """calendar_posts.lineup_json (2026-09-02): the posted rows, so a
    refresh never downgrades a priced move to a dash. Idempotent."""
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(calendar_posts)")}
        if cols and "lineup_json" not in cols:
            conn.execute("ALTER TABLE calendar_posts ADD COLUMN lineup_json TEXT")
            conn.commit()
    except Exception as e:  # never block boot
        log.warning(f"calendar_posts.lineup_json migration skipped: {e}")


def _migrate_pdf_query_surface(conn) -> None:
    """Make the PDF research data queryable by a text-to-SQL bot
    (2026-07-29 storage review). Idempotent — safe on every boot.

    Before this, ~28 structured fields per PDF lived inside the
    `analysis_json` blob with exactly ONE real column (priority):
    source/type filtering meant json_extract full scans, ticker search
    meant an unindexable json_each cross-join, and the append-only
    MAX(id) dedup silently double-counted the 375 PDFs with more than
    one analysis.

    Adds:
      * GENERATED columns source / report_type / title — computed from
        analysis_json, so zero backfill and no writer change, and they
        index like normal columns.
      * Indexes for the query shapes a research bot actually uses.
      * `latest_pdf_analyses` VIEW — pre-deduped (latest per PDF) and
        pre-joined to pdf_files, exposing file_name + a REAL date as
        `published_at` (analysis_json.published_at is null in every row:
        it's only populated on read-back, never at insert).
      * `pdf_entities` child table — one row per mentioned ticker,
        indexed, so "which reports mention NVDA" is an indexed lookup
        instead of a full-scan JSON cross-join.
    """
    # --- generated columns (ALTER ADD COLUMN supports VIRTUAL only) ---
    existing = {r[1] for r in conn.execute(
        "PRAGMA table_info(pdf_analyses)").fetchall()}
    for col, path in (
        ("source", "$.source"),
        ("report_type", "$.report_type"),
        ("title", "$.title"),
    ):
        if col in existing:
            continue
        try:
            conn.execute(
                f"ALTER TABLE pdf_analyses ADD COLUMN {col} TEXT "
                f"GENERATED ALWAYS AS (json_extract(analysis_json, "
                f"'{path}')) VIRTUAL"
            )
        except Exception as e:
            log.warning(f"pdf query surface: add column {col} failed: {e}")

    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_pdf_analyses_source
            ON pdf_analyses(source);
        CREATE INDEX IF NOT EXISTS idx_pdf_analyses_report_type
            ON pdf_analyses(report_type);
        CREATE INDEX IF NOT EXISTS idx_pdf_analyses_priority
            ON pdf_analyses(priority);
        -- serves the latest-per-PDF lookup
        CREATE INDEX IF NOT EXISTS idx_pdf_analyses_latest
            ON pdf_analyses(pdf_file_id, id DESC);

        CREATE VIEW IF NOT EXISTS latest_pdf_analyses AS
            SELECT
                pa.id            AS analysis_id,
                pa.pdf_file_id   AS pdf_file_id,
                pa.source        AS source,
                pa.report_type   AS report_type,
                pa.title         AS title,
                pa.priority      AS priority,
                pf.file_name     AS file_name,
                pf.dropbox_path  AS dropbox_path,
                COALESCE(pf.dropbox_modified_at, pa.created_at)
                                 AS published_at,
                pa.created_at    AS analyzed_at,
                pa.analysis_json AS analysis_json
            FROM pdf_analyses pa
            JOIN pdf_files pf ON pf.id = pa.pdf_file_id
            WHERE pa.id = (
                SELECT MAX(id) FROM pdf_analyses
                WHERE pdf_file_id = pa.pdf_file_id
            );

        -- Honest trade scoreboard (2026-07-30). The ledger is
        -- WINS-BIASED: gain_pct only exists where someone POSTED a
        -- close, and members screenshot winners while silently
        -- abandoning losers. A naive
        --   wins / COUNT(gain_pct IS NOT NULL)
        -- therefore prints 96-100% "win rates" (observed: Sam 100% on
        -- 8 documented wins while 44 of his trades were opened and
        -- never closed — 15% once those count as losses). The tool
        -- description warned about this and the model wrote the naive
        -- query anyway, so the honest math lives in SQL where it can't
        -- be skipped. Column names carry the caveat.
        -- DROP first: the name-grouped version of this view already
        -- shipped, and CREATE VIEW IF NOT EXISTS would leave it in
        -- place. Views hold no data, so dropping is free.
        DROP VIEW IF EXISTS trade_scoreboard;

        -- Grouped by author_id, NOT by name: this room renames itself
        -- constantly, so one trader shows up under many display names
        -- (423994649317736448 = 'BK' + 'M&AK' + 'bearishkyle';
        -- 1192771108332650496 = 'abe' + 'abugs bunny' + 'abullish_xyz'
        -- + 'abearish' + ...). Name-based grouping split BK's 184
        -- trades into three separate "traders" of 81/73/21 and
        -- understated everyone (2026-07-30). author_id is populated on
        -- 885 of 887 trades; the 2 stragglers fall back to the name.
        CREATE VIEW IF NOT EXISTS trade_scoreboard AS
            SELECT
                COALESCE(CAST(author_id AS TEXT),
                         caller, author)           AS trader_key,
                (SELECT t2.author FROM analyst_trades t2
                  WHERE COALESCE(CAST(t2.author_id AS TEXT), t2.caller,
                                 t2.author)
                        = COALESCE(CAST(t.author_id AS TEXT), t.caller,
                                   t.author)
                    AND t2.is_trade = 1
                  ORDER BY t2.posted_at DESC LIMIT 1)
                                                   AS trader,
                COUNT(*)                           AS logged_trades,
                SUM(CASE WHEN gain_pct > 0 THEN 1 ELSE 0 END)
                                                   AS documented_wins,
                SUM(CASE WHEN gain_pct <= 0 THEN 1 ELSE 0 END)
                                                   AS documented_losses,
                -- A close with no percentage is CLOSED, just unscored
                -- ("sold DELL way too early smh"). 179 of the room's
                -- 431 closes look like this. Calling them never_closed
                -- claims the position is still open, which is false.
                -- Price-scoring them recovers only 4 rows — opens
                -- almost never record a price — so they get their own
                -- bucket rather than a guess.
                SUM(CASE WHEN gain_pct IS NULL
                          AND action IN ('close', 'trim')
                         THEN 1 ELSE 0 END)         AS closed_unscored,
                SUM(CASE WHEN gain_pct IS NULL
                          AND (action IS NULL
                               OR action NOT IN ('close', 'trim'))
                         THEN 1 ELSE 0 END)         AS never_closed,
                ROUND(100.0 * SUM(CASE WHEN gain_pct > 0 THEN 1 ELSE 0 END)
                      / NULLIF(SUM(CASE WHEN gain_pct IS NOT NULL
                                        THEN 1 ELSE 0 END), 0), 1)
                                   AS win_rate_BIASED_documented_only,
                -- The fair middle: divide by positions that are
                -- actually CLOSED (scored + unscored), so a posted
                -- exit with no number counts against you but a trade
                -- still running does not. Generous on options, where
                -- most never_closed rows are probably expirations —
                -- quote it WITH never_closed, never on its own.
                ROUND(100.0 * SUM(CASE WHEN gain_pct > 0 THEN 1 ELSE 0 END)
                      / NULLIF(SUM(CASE WHEN gain_pct IS NOT NULL
                                          OR action IN ('close', 'trim')
                                        THEN 1 ELSE 0 END), 0), 1)
                                   AS win_rate_closed_positions_only,
                ROUND(100.0 * SUM(CASE WHEN gain_pct > 0 THEN 1 ELSE 0 END)
                      / NULLIF(COUNT(*), 0), 1)
                                   AS win_rate_honest_ghosts_as_losses,
                ROUND(AVG(CASE WHEN gain_pct > 0 THEN gain_pct END), 1)
                                   AS avg_gain_on_wins_only
            FROM analyst_trades t
            WHERE is_trade = 1
            GROUP BY 1;

        CREATE TABLE IF NOT EXISTS pdf_entities (
            analysis_id INTEGER NOT NULL,
            pdf_file_id INTEGER NOT NULL,
            ticker      TEXT,
            name        TEXT,
            asset_class TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_pdf_entities_ticker
            ON pdf_entities(ticker);
        CREATE INDEX IF NOT EXISTS idx_pdf_entities_analysis
            ON pdf_entities(analysis_id);
    """)

    # --- backfill pdf_entities for analyses not yet represented ---
    # Bounded per boot so a cold start can't stall; runs to completion
    # across a few boots on a fresh DB, instantly once caught up.
    try:
        rows = conn.execute(
            """SELECT pa.id, pa.pdf_file_id, pa.analysis_json
               FROM pdf_analyses pa
               WHERE pa.analysis_json IS NOT NULL
                 AND NOT EXISTS (
                     SELECT 1 FROM pdf_entities e
                     WHERE e.analysis_id = pa.id
                 )
               ORDER BY pa.id DESC
               LIMIT 5000"""
        ).fetchall()
    except Exception as e:
        log.warning(f"pdf_entities backfill query failed: {e}")
        rows = []

    if rows:
        import json as _json
        payload = []
        for r in rows:
            aid, fid, blob = r[0], r[1], r[2]
            try:
                data = _json.loads(blob) or {}
            except Exception:
                continue
            ents = data.get("entities_mentioned") or []
            if not isinstance(ents, list):
                continue
            seen = set()
            for e in ents:
                if not isinstance(e, dict):
                    continue
                tk = (e.get("ticker") or "").strip().upper()
                nm = (e.get("name") or "").strip()
                if not tk and not nm:
                    continue
                if tk in seen and tk:
                    continue
                seen.add(tk)
                payload.append((
                    aid, fid, tk or None, nm[:120] or None,
                    (e.get("asset_class") or "").strip()[:20] or None,
                ))
            if not ents:
                # Mark as processed with a null-ticker sentinel so the
                # NOT EXISTS gate doesn't re-scan this row every boot.
                payload.append((aid, fid, None, None, None))
        if payload:
            conn.executemany(
                "INSERT INTO pdf_entities "
                "(analysis_id, pdf_file_id, ticker, name, asset_class) "
                "VALUES (?, ?, ?, ?, ?)",
                payload,
            )
            log.info(
                f"pdf_entities backfill: {len(payload)} rows from "
                f"{len(rows)} analyses"
            )
    conn.commit()


# Shared subquery: pick the latest analysis row per pdf_file_id.
# Since a single PDF can have multiple analyses (e.g., reprocessed), we always
# want the most recent one by id.
_LATEST_ANALYSIS_CTE = """
    WITH latest_analyses AS (
        SELECT pa.*
        FROM pdf_analyses pa
        WHERE pa.id = (
            SELECT MAX(id) FROM pdf_analyses
            WHERE pdf_file_id = pa.pdf_file_id
        )
    )
"""


def _normalize_ts(ts: str | None) -> str | None:
    """Normalize a timestamp string to ISO 'T' format for cross-format comparison.

    SQLite's datetime('now') produces 'YYYY-MM-DD HH:MM:SS' (space separator).
    Python's datetime.isoformat() produces 'YYYY-MM-DDTHH:MM:SS' (T separator).
    Lexicographic TEXT comparison treats these differently (T > space), which
    breaks any cutoff query that mixes the two formats.

    Parse with fromisoformat (handles both) and re-emit as T-format so comparisons
    line up with dropbox_modified_at (which is always T-format).
    """
    if not ts:
        return ts
    try:
        return datetime.fromisoformat(ts.replace(" ", "T", 1)).isoformat()
    except (ValueError, TypeError):
        return ts


# --- Daily reports ---



# ---------------------------------------------------------------------------
# Opus-bridge HIGH ingestion state machine
# ---------------------------------------------------------------------------



# =============================================================================
# Reanalyze job state (persistent background-processing for /reanalyze).
# =============================================================================


# =============================================================================
# Daily pulse / synthesis history.
# =============================================================================


# --- Processing log ---



# --- Stats ---



# =============================================================================
# /ask Perplexity rate-limit log.
# =============================================================================


# =============================================================================
# Format-overhaul Phase 1: pulse_state + pulse_leans (WHAT CHANGED / TRADE BOARD)
# =============================================================================


# Trade-statement signal in free chat — for the "how did I do" lookup.
# Most of the room calls trades by TALKING ("meta put at open 100%",
# "buy puts", "fomc shorts"), not by posting screenshots, so the
# OCR'd analyst_trades ledger is empty for them and the bot was telling
# active traders "you did nothing / batting .000" (observed 2026-06-17:
# terlin called META puts +100% in chat, got "zero mentions of META").
# This surfaces those self-reported chat trades alongside the ledger.
# Precision-favoring: strong trade verbs, plural contracts, ticker/number
# + call/put, dollar levels, percentages, dte. Avoids matching bare
# "call me" / "put it down" (singular call/put without trade context).
_CHAT_TRADE_RE = re.compile(
    r"("
    r"\b((?:re)?bought|(?:re)?sold|short(?:s|ed|ing)?|long(?:s|ed|ing)?|"
    r"scalp(?:ed|ing)?|runner|trim(?:med)?|hedged?|assigned|rolled|\d?dte)\b"
    r"|\bputs\b|\bcalls\b"
    r"|\$[A-Za-z]{1,5}\s+(?:call|put)s?\b"
    r"|\b\d{2,5}\s?(?:c|p|call|put)s?\b"
    r"|\$\d"
    r"|[-+]?\d+(?:\.\d+)?%"
    r"|\bbuy\s+(?:puts?|calls?)\b"
    r")",
    re.IGNORECASE,
)


# =============================================================================
# Reminder dedup (channel reminder system — reminders/job.py).
# =============================================================================


# =============================================================================
# Analyst trade log (populated by analyst_log.watcher).
# =============================================================================


# =============================================================================
# User profiles (LLM-generated personality summaries).
# =============================================================================


_PROFILES_BLOCK_BUDGET_CHARS = 18000  # cap WHO'S TALKING block
_PROFILES_BLOCK_PER_USER_CHARS = 2500  # cap each individual profile
_PROFILES_BLOCK_MAX_USERS = 15         # hard ceiling on user count


_PROFILE_SECTION_SPLIT_RE = __import__("re").compile(
    r"(?=\*\*(?:Personality and style|Voice|Retarded takes|Recent trades|"
    r"Recent personal life)\.\*\*)",
)


# Ledger constants (2026-07-01): scoring window widened to 21d with a
# recency band — wins ≤7d old score 2 pts, 8..21d score 1 pt. Ghosting
# stays a fixed 14d judgment about the position, independent of window.
_RECENT_WIN_DAYS = 7
_GHOST_AGE_DAYS = 14


# ---------------------------------------------------------------------
# Sleeper fantasy player cache (2026-08-20)
# ---------------------------------------------------------------------

SLEEPER_DUMP_MIN_ROWS = 1000  # anomaly guard threshold, patchable in tests


# ---------------------------------------------------------------------
# Symbol market-cap cache (2026-08-20, daily calendar graphic)
# ---------------------------------------------------------------------



# ---------------------------------------------------------------------
# Chat-catchup watermark + VACUUM (2026-08-23, memory/cost fixes)
# ---------------------------------------------------------------------


# ----------------------------------------------------------------------
# Facade (2026-09-01): db.py was 6.3k lines in one file. Subject modules
# live in db_parts/ and every name is re-exported here, so callers,
# tests and smokes keep using `db.<name>`.
from db_parts.analyst import (  # noqa: E402,F401
    _find_inheritable_entry,
    analyst_trade_exists,
    backfill_orphan_exit_links,
    compute_caller_win_loss_summary,
    compute_member_points,
    find_matching_open_expiry,
    find_recent_book_posts,
    get_analyst_trade_by_message_id,
    get_bot_book_post,
    get_current_analyst_positions,
    get_latest_analyst_trade_posted_at,
    get_member_trade_events,
    get_recent_analyst_trades,
    insert_text_extracted_trade_if_not_dup,
    known_trade_caller_names,
    mark_expired_analyst_positions,
    prune_bot_book_posts,
    purge_old_expired_analyst_trades,
    record_analyst_trade,
    record_bot_book_post,
)
from db_parts.ask import (  # noqa: E402,F401
    count_ask_queries_today_for_user,
    get_recent_bot_answers_to_asker,
    record_ask_bot_answer,
    record_ask_query,
)
from db_parts.chat import (  # noqa: E402,F401
    count_chat_messages_for_channels,
    export_user_profiles_markdown,
    find_oldest_chat_gap,
    find_user_messages_matching,
    find_users_mentioned_in_text,
    get_catchup_watermark,
    get_chat_message_row,
    get_global_trader_ranks,
    get_latest_chat_message_posted_at,
    get_profiles_for_users,
    get_promoted_protected_ids,
    get_recent_user_chat_trades,
    get_recent_user_messages,
    get_sleeper_player_names,
    get_user_profile,
    get_user_profile_by_username,
    get_user_profile_recent_trades_section,
    is_official_caller,
    load_chat_messages_for_profiles,
    lookup_user_ranks,
    maybe_promote_protected,
    prune_user_profiles_to_top_n,
    purge_old_chat_messages,
    resolve_username_to_user_id,
    search_chat_messages_for_ask,
    set_catchup_watermark,
    set_chat_image_ocr,
    sleeper_players_cache_age_hours,
    store_chat_message,
    upsert_sleeper_players,
    upsert_user_profile,
)
from db_parts.pdf import (  # noqa: E402,F401
    recently_covered_tickers,
    _index_analysis_entities,
    _today_utc_range,
    clear_pending_queue,
    complete_reanalyze_job,
    count_bridge_outcomes_since,
    count_pending_announcements,
    count_pending_queue,
    create_reanalyze_job,
    fail_reanalyze_job,
    find_timed_out_bridge_committed,
    get_active_reanalyze_job,
    get_analyses_between,
    get_analyses_since,
    get_bridge_state,
    get_committed_bridge_pdfs,
    get_dropbox_cursor,
    get_failed_pdfs_for_retry,
    get_fallback_bridge_pdfs,
    get_ingest_feed_last_announced,
    get_latest_dropbox_modified_at,
    get_next_pdf_to_announce,
    get_pdf_by_path,
    get_pending_bridge_pdfs,
    get_pending_pdfs,
    get_pipeline_stats,
    get_reanalyze_job,
    get_recent_pipeline_events,
    get_recent_reanalyze_jobs,
    get_today_stats,
    get_todays_analyses,
    insert_analysis,
    insert_pdf_file,
    log_event,
    max_announceable_pdf_file_id,
    queue_for_opus_bridge,
    record_pipeline_event,
    reset_stale_processing,
    run_retention_purge,
    set_ingest_feed_last_announced,
    start_reanalyze_job,
    update_bridge_status,
    update_dropbox_cursor,
    update_pdf_priority,
    update_pdf_status,
    update_reanalyze_job_progress,
    watchdog_already_alerted,
)
from db_parts.pulse import (  # noqa: E402,F401
    calendar_already_posted,
    count_recent_reversals,
    daily_pulse_delivered_on,
    find_sent_report_by_pending_file,
    get_board_leans,
    get_calendar_posts,
    get_last_daily_pulse,
    get_last_daily_pulses,
    get_last_report_time,
    get_market_caps,
    get_prev_scheduled_pulse_date,
    get_prev_stamped_pulse_state,
    get_recent_daily_pulse_titles,
    get_symbol_logos,
    insert_daily_report,
    mark_calendar_refreshed,
    mark_reminder_sent,
    mark_report_sent,
    record_calendar_posts,
    reminder_already_sent,
    save_pulse_state_candidate,
    stamp_pulse_state_for_date,
    upsert_market_caps,
    upsert_pulse_leans,
    upsert_symbol_logos,
)
from db_parts.summaries import (  # noqa: E402,F401
    _reorder_profile_for_roast_attention,
    append_ask_interaction,
    compute_abe_win_loss_summary,
    format_analyst_trades_for_context,
    format_user_profiles_for_context,
    receipts_ceiling_from_points,
    recompute_trader_ranks_on_profiles,
    vacuum_db,
)
