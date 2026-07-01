"""SQLite database setup and query helpers."""

import json
import logging
import re
import sqlite3
from datetime import datetime, date, timedelta
from pathlib import Path

from config import settings

log = logging.getLogger(__name__)

_conn: sqlite3.Connection | None = None


def get_connection() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        db_path = Path(settings.db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(db_path), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _init_schema(_conn)
        _migrate_drop_unique_constraints(_conn)
        _migrate_add_extraction_source(_conn)
    return _conn


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
    # Backfill caller='abe' for all pre-existing rows (single-caller era).
    # Safe to run repeatedly — only updates rows where caller is currently
    # NULL. New rows write caller explicitly so this only ever touches
    # legacy data.
    try:
        conn.execute(
            "UPDATE analyst_trades SET caller = 'abe' WHERE caller IS NULL"
        )
    except sqlite3.OperationalError:
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


def _migrate_add_extraction_source(conn: sqlite3.Connection) -> None:
    """Add extraction_source column to analyst_trades (2026-06-02).

    Tracks which modality produced each row:
      - 'image' : image-OCR pipeline (the original path)
      - 'text'  : text classifier (no image attachments)
      - 'mixed' : classifier consumed both text + image evidence

    Idempotent: PRAGMA-checks for the column before ALTER. Backfills
    any NULL values to 'image' (every legacy row came from the image-OCR
    pipeline; the column didn't exist before 2026-06-02). Safe to run
    on every connection boot — already-migrated rows produce no UPDATE
    activity.
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(analyst_trades)").fetchall()]
    if "extraction_source" not in cols:
        conn.execute("ALTER TABLE analyst_trades ADD COLUMN extraction_source TEXT")
    # Backfill NULL → 'image'. Idempotent.
    conn.execute(
        "UPDATE analyst_trades SET extraction_source = 'image' "
        "WHERE extraction_source IS NULL"
    )
    conn.commit()


# --- Dropbox state ---

def get_dropbox_cursor() -> str | None:
    row = get_connection().execute("SELECT cursor FROM dropbox_state WHERE id = 1").fetchone()
    return row["cursor"] if row else None


def update_dropbox_cursor(cursor: str) -> None:
    conn = get_connection()
    conn.execute(
        "UPDATE dropbox_state SET cursor = ?, last_poll_at = ? WHERE id = 1",
        (cursor, datetime.utcnow().isoformat()),
    )
    conn.commit()


# --- PDF files ---

def insert_pdf_file(
    dropbox_path: str,
    file_name: str,
    local_path: str,
    dropbox_rev: str | None = None,
    file_size_bytes: int | None = None,
    dropbox_modified_at: str | None = None,
) -> int:
    conn = get_connection()
    cur = conn.execute(
        """INSERT OR IGNORE INTO pdf_files
           (dropbox_path, file_name, local_path, dropbox_rev, file_size_bytes,
            dropbox_modified_at, downloaded_at, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'DOWNLOADED')""",
        (dropbox_path, file_name, local_path, dropbox_rev, file_size_bytes,
         dropbox_modified_at, datetime.utcnow().isoformat()),
    )
    conn.commit()
    return cur.lastrowid


def get_pdf_by_path(dropbox_path: str) -> dict | None:
    row = get_connection().execute(
        "SELECT * FROM pdf_files WHERE dropbox_path = ?", (dropbox_path,)
    ).fetchone()
    return dict(row) if row else None


def get_pending_pdfs(limit: int = 50) -> list[dict]:
    rows = get_connection().execute(
        """SELECT * FROM pdf_files
           WHERE status = 'DOWNLOADED'
           ORDER BY file_size_bytes ASC, created_at ASC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_failed_pdfs_for_retry(max_retries: int = 3) -> list[dict]:
    rows = get_connection().execute(
        """SELECT * FROM pdf_files
           WHERE status = 'FAILED' AND retry_count < ?
           ORDER BY created_at ASC""",
        (max_retries,),
    ).fetchall()
    return [dict(r) for r in rows]


def update_pdf_status(pdf_id: int, status: str, error_message: str | None = None) -> None:
    conn = get_connection()
    if error_message:
        conn.execute(
            """UPDATE pdf_files
               SET status = ?, error_message = ?, retry_count = retry_count + 1,
                   updated_at = datetime('now')
               WHERE id = ?""",
            (status, error_message, pdf_id),
        )
    else:
        conn.execute(
            "UPDATE pdf_files SET status = ?, updated_at = datetime('now') WHERE id = ?",
            (status, pdf_id),
        )
    conn.commit()


def update_pdf_priority(pdf_id: int, priority: str) -> None:
    conn = get_connection()
    conn.execute(
        "UPDATE pdf_files SET priority = ?, updated_at = datetime('now') WHERE id = ?",
        (priority, pdf_id),
    )
    conn.commit()


def count_pending_queue() -> int:
    """Return how many PDFs are currently in the pending queue."""
    row = get_connection().execute(
        "SELECT COUNT(*) as c FROM pdf_files WHERE status IN ('DOWNLOADED', 'PROCESSING')"
    ).fetchone()
    return row["c"] if row else 0


def clear_pending_queue() -> int:
    """Delete all DOWNLOADED/PROCESSING rows from pdf_files and their local files.

    Returns the number of rows deleted. Caller is responsible for any safety checks.
    """
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, local_path FROM pdf_files WHERE status IN ('DOWNLOADED', 'PROCESSING')"
    ).fetchall()
    pending_ids = [r["id"] for r in rows]

    if not pending_ids:
        return 0

    # Remove local PDF files from disk
    for r in rows:
        if r["local_path"]:
            try:
                Path(r["local_path"]).unlink(missing_ok=True)
            except Exception:
                pass

    placeholders = ",".join("?" * len(pending_ids))
    conn.execute(f"DELETE FROM processing_log WHERE pdf_file_id IN ({placeholders})", pending_ids)
    conn.execute(f"DELETE FROM pdf_analyses WHERE pdf_file_id IN ({placeholders})", pending_ids)
    conn.execute(f"DELETE FROM pdf_files WHERE id IN ({placeholders})", pending_ids)
    conn.commit()
    return len(pending_ids)


# --- Analyses ---

def insert_analysis(
    pdf_file_id: int,
    triage_json: str | None,
    analysis_json: str | None,
    priority: str,
    pages_analyzed: int,
    total_pages: int,
    input_tokens: int,
    output_tokens: int,
    model: str,
    duration: float,
) -> int:
    conn = get_connection()
    cur = conn.execute(
        """INSERT INTO pdf_analyses
           (pdf_file_id, triage_json, analysis_json, priority, pages_analyzed,
            total_pages, input_tokens_used, output_tokens_used, model_used,
            analysis_duration_seconds)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (pdf_file_id, triage_json, analysis_json, priority, pages_analyzed,
         total_pages, input_tokens, output_tokens, model, duration),
    )
    conn.commit()
    return cur.lastrowid


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


def get_todays_analyses(today: str | None = None) -> list[dict]:
    if today is None:
        today = date.today().isoformat()
    rows = get_connection().execute(
        _LATEST_ANALYSIS_CTE + """
        SELECT la.*, pf.file_name, pf.dropbox_path, pf.dropbox_modified_at
        FROM latest_analyses la
        JOIN pdf_files pf ON la.pdf_file_id = pf.id
        WHERE date(la.created_at) = ?
        ORDER BY la.created_at ASC""",
        (today,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_analyses_since(since_time: str) -> list[dict]:
    """Get latest analysis per PDF where the PDF was uploaded to Dropbox after since_time."""
    since_time = _normalize_ts(since_time)
    rows = get_connection().execute(
        _LATEST_ANALYSIS_CTE + """
        SELECT la.*, pf.file_name, pf.dropbox_path, pf.dropbox_modified_at
        FROM latest_analyses la
        JOIN pdf_files pf ON la.pdf_file_id = pf.id
        WHERE pf.dropbox_modified_at > ?
        ORDER BY pf.dropbox_modified_at ASC""",
        (since_time,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_analyses_between(start_time: str, end_time: str) -> list[dict]:
    """Get latest analysis per PDF where the PDF was uploaded to Dropbox within a window."""
    start_time = _normalize_ts(start_time)
    end_time = _normalize_ts(end_time)
    rows = get_connection().execute(
        _LATEST_ANALYSIS_CTE + """
        SELECT la.*, pf.file_name, pf.dropbox_path, pf.dropbox_modified_at
        FROM latest_analyses la
        JOIN pdf_files pf ON la.pdf_file_id = pf.id
        WHERE pf.dropbox_modified_at >= ? AND pf.dropbox_modified_at <= ?
        ORDER BY pf.dropbox_modified_at ASC""",
        (start_time, end_time),
    ).fetchall()
    return [dict(r) for r in rows]


# --- Daily reports ---

def insert_daily_report(
    report_date: str,
    report_type: str,
    report_json: str,
    report_markdown: str,
    pdf_count: int,
    input_tokens: int,
    output_tokens: int,
) -> int:
    conn = get_connection()
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


def mark_report_sent(report_id: int) -> None:
    conn = get_connection()
    conn.execute(
        "UPDATE daily_reports SET discord_sent_at = datetime('now') WHERE id = ?",
        (report_id,),
    )
    conn.commit()


def get_last_report_time() -> str | None:
    """Get the created_at timestamp of the most recent daily report, any date."""
    row = get_connection().execute(
        """SELECT created_at FROM daily_reports
           WHERE report_type = 'daily'
           ORDER BY created_at DESC LIMIT 1"""
    ).fetchone()
    return row["created_at"] if row else None


def get_ingest_feed_last_announced() -> int:
    """Highest pdf_file_id we've already posted to the ingestion feed."""
    row = get_connection().execute(
        "SELECT last_announced_pdf_file_id FROM ingest_feed_state WHERE id = 1"
    ).fetchone()
    return int(row["last_announced_pdf_file_id"]) if row else 0


def set_ingest_feed_last_announced(pdf_file_id: int) -> None:
    conn = get_connection()
    conn.execute(
        """UPDATE ingest_feed_state
           SET last_announced_pdf_file_id = ?, updated_at = datetime('now')
           WHERE id = 1""",
        (int(pdf_file_id),),
    )
    conn.commit()


def get_next_pdf_to_announce(after_pdf_file_id: int) -> dict | None:
    """Next analyzed HIGH/MEDIUM PDF whose pdf_file_id > after_pdf_file_id.

    Joins pdf_files with the LATEST pdf_analyses row per file (so reanalyses
    are deduped). Skips LOW priority. Returns dict or None if none pending.
    """
    row = get_connection().execute(
        _LATEST_ANALYSIS_CTE + """
        SELECT la.*, pf.id AS pf_id, pf.file_name, pf.dropbox_path,
               pf.dropbox_modified_at, pf.file_size_bytes
        FROM latest_analyses la
        JOIN pdf_files pf ON la.pdf_file_id = pf.id
        WHERE la.priority IN ('high', 'medium')
          AND pf.id > ?
        ORDER BY pf.id ASC
        LIMIT 1""",
        (int(after_pdf_file_id),),
    ).fetchone()
    return dict(row) if row else None


def count_pending_announcements(after_pdf_file_id: int) -> dict:
    """Count HIGH/MEDIUM PDFs queued for announcement after the given id.

    Used at startup to decide whether to trickle or post a backfill summary.
    Returns {'high': N, 'medium': M, 'total': N+M}.
    """
    rows = get_connection().execute(
        _LATEST_ANALYSIS_CTE + """
        SELECT la.priority, COUNT(*) as n
        FROM latest_analyses la
        JOIN pdf_files pf ON la.pdf_file_id = pf.id
        WHERE la.priority IN ('high', 'medium')
          AND pf.id > ?
        GROUP BY la.priority""",
        (int(after_pdf_file_id),),
    ).fetchall()
    out = {"high": 0, "medium": 0, "total": 0}
    for r in rows:
        out[r["priority"]] = int(r["n"])
        out["total"] += int(r["n"])
    return out


def max_announceable_pdf_file_id() -> int:
    """Current MAX(pdf_file_id) among HIGH/MEDIUM analyzed PDFs.

    Used at startup to fast-forward last_announced past a backfilled batch.
    """
    row = get_connection().execute(
        _LATEST_ANALYSIS_CTE + """
        SELECT COALESCE(MAX(pf.id), 0) AS m
        FROM latest_analyses la
        JOIN pdf_files pf ON la.pdf_file_id = pf.id
        WHERE la.priority IN ('high', 'medium')"""
    ).fetchone()
    return int(row["m"]) if row else 0


# ---------------------------------------------------------------------------
# Opus-bridge HIGH ingestion state machine
# ---------------------------------------------------------------------------

def queue_for_opus_bridge(pdf_file_id: int) -> None:
    """Insert (or reset) a bridge-state row for a HIGH PDF.

    Idempotent: if a row already exists for this pdf_file_id (e.g. from a
    prior reanalyze that fell back to Gemini), it's reset to status='pending'
    with a fresh queued_at.
    """
    get_connection().execute(
        """INSERT INTO bridge_ingestion_state
             (pdf_file_id, status, queued_at)
           VALUES (?, 'pending', datetime('now'))
           ON CONFLICT(pdf_file_id) DO UPDATE SET
             status = 'pending',
             queued_at = datetime('now'),
             committed_at = NULL,
             completed_at = NULL,
             bridge_filename = NULL,
             error_message = NULL,
             fallback_reason = NULL""",
        (int(pdf_file_id),),
    )
    get_connection().commit()


def get_bridge_state(pdf_file_id: int) -> dict | None:
    row = get_connection().execute(
        "SELECT * FROM bridge_ingestion_state WHERE pdf_file_id = ?",
        (int(pdf_file_id),),
    ).fetchone()
    return dict(row) if row else None


def update_bridge_status(
    pdf_file_id: int,
    status: str,
    bridge_filename: str | None = None,
    bridge_commit_sha: str | None = None,
    error_message: str | None = None,
    fallback_reason: str | None = None,
    increment_attempt: bool = False,
) -> None:
    """Update a bridge state row. Auto-stamps committed_at/completed_at by status."""
    conn = get_connection()
    now = "datetime('now')"
    sets = ["status = ?"]
    params: list = [status]
    if status == "committed":
        sets.append(f"committed_at = {now}")
    if status in ("completed", "fallback_to_gemini", "failed"):
        sets.append(f"completed_at = {now}")
    if bridge_filename is not None:
        sets.append("bridge_filename = ?")
        params.append(bridge_filename)
    if bridge_commit_sha is not None:
        sets.append("bridge_commit_sha = ?")
        params.append(bridge_commit_sha)
    if error_message is not None:
        sets.append("error_message = ?")
        params.append(error_message[:500])
    if fallback_reason is not None:
        sets.append("fallback_reason = ?")
        params.append(fallback_reason[:200])
    if increment_attempt:
        sets.append("attempt_count = attempt_count + 1")
    params.append(int(pdf_file_id))
    conn.execute(
        f"UPDATE bridge_ingestion_state SET {', '.join(sets)} WHERE pdf_file_id = ?",
        params,
    )
    conn.commit()


def get_pending_bridge_pdfs(limit: int = 50) -> list[dict]:
    """Bridge-state rows in 'pending' status — not yet committed to GitHub."""
    rows = get_connection().execute(
        """SELECT b.*, pf.file_name, pf.dropbox_path, pf.local_path,
                  pf.dropbox_modified_at, pf.file_size_bytes
           FROM bridge_ingestion_state b
           JOIN pdf_files pf ON pf.id = b.pdf_file_id
           WHERE b.status = 'pending'
           ORDER BY b.queued_at ASC
           LIMIT ?""",
        (int(limit),),
    ).fetchall()
    return [dict(r) for r in rows]


def get_committed_bridge_pdfs(limit: int = 100) -> list[dict]:
    """Rows committed to the bridge but not yet processed by the Opus routine.

    Used by the pull job (to check for completed results) and by the
    timeout watchdog (to fall back to Gemini after opus_bridge_timeout_minutes).
    """
    rows = get_connection().execute(
        """SELECT b.*, pf.file_name, pf.dropbox_path
           FROM bridge_ingestion_state b
           JOIN pdf_files pf ON pf.id = b.pdf_file_id
           WHERE b.status = 'committed'
           ORDER BY b.committed_at ASC
           LIMIT ?""",
        (int(limit),),
    ).fetchall()
    return [dict(r) for r in rows]


def get_fallback_bridge_pdfs(limit: int = 100) -> list[dict]:
    """Rows marked fallback_to_gemini but not yet completed via Gemini.

    Used by the fallback sweeper to actually run Gemini deep-analysis on
    PDFs that hit guardrails or routine failures. After Gemini succeeds,
    the row is updated to status='completed'.
    """
    rows = get_connection().execute(
        """SELECT b.*, pf.file_name, pf.dropbox_path, pf.local_path,
                  pf.dropbox_modified_at, pf.file_size_bytes
           FROM bridge_ingestion_state b
           JOIN pdf_files pf ON pf.id = b.pdf_file_id
           WHERE b.status = 'fallback_to_gemini'
           ORDER BY b.queued_at ASC
           LIMIT ?""",
        (int(limit),),
    ).fetchall()
    return [dict(r) for r in rows]


def find_timed_out_bridge_committed(timeout_iso: str, limit: int = 100) -> list[dict]:
    """Rows committed to the bridge before `timeout_iso` that haven't
    progressed to 'completed' or 'fallback_to_gemini' or 'failed'.

    These are PDFs the routine apparently never got to (or whose result
    wasn't pulled). The watchdog converts them to fallback_to_gemini so
    the sweeper can run Gemini and produce a pdf_analyses row.

    `timeout_iso` should be normalized to T-format before passing
    (datetime('now') uses space separator — _normalize_ts handles).
    """
    cutoff = _normalize_ts(timeout_iso) or timeout_iso
    rows = get_connection().execute(
        """SELECT b.*, pf.file_name, pf.dropbox_path
           FROM bridge_ingestion_state b
           JOIN pdf_files pf ON pf.id = b.pdf_file_id
           WHERE b.status = 'committed'
             AND b.committed_at IS NOT NULL
             AND b.committed_at < ?
           ORDER BY b.committed_at ASC
           LIMIT ?""",
        (cutoff, int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]


def count_bridge_outcomes_since(cutoff_iso: str) -> dict:
    """Count bridge-ingestion outcomes since a given ISO timestamp.

    Used by /status to surface bridge health (attempted, completed via Opus,
    fell back to Gemini, hard failures).

    Normalizes the cutoff to T-format so it lexically compares correctly
    against queued_at (which is written by SQLite's datetime('now') in
    space-separator format). See _normalize_ts() docstring for the full
    landmine writeup.
    """
    cutoff = _normalize_ts(cutoff_iso) or cutoff_iso
    rows = get_connection().execute(
        """SELECT status, COUNT(*) AS n
           FROM bridge_ingestion_state
           WHERE queued_at > ?
           GROUP BY status""",
        (cutoff,),
    ).fetchall()
    out = {
        "total": 0,
        "pending": 0,
        "committed": 0,
        "completed": 0,
        "fallback_to_gemini": 0,
        "failed": 0,
    }
    for r in rows:
        s = r["status"]
        n = int(r["n"])
        if s in out:
            out[s] = n
        out["total"] += n
    return out


# =============================================================================
# Reanalyze job state (persistent background-processing for /reanalyze).
# =============================================================================


def create_reanalyze_job(
    hours: int,
    target_pdf_ids: list[int],
    priority_filter: list[str] | None,
    requested_by: str | None = None,
    discord_channel_id: int | None = None,
    discord_status_message_id: int | None = None,
) -> int:
    """Insert a new reanalyze job in `pending` state. Returns the job id."""
    import json as _json
    cur = get_connection().execute(
        """INSERT INTO reanalyze_jobs
           (status, hours, priority_filter, target_pdf_ids, target_count,
            requested_by, discord_channel_id, discord_status_message_id)
           VALUES ('pending', ?, ?, ?, ?, ?, ?, ?)""",
        (
            hours,
            _json.dumps(priority_filter) if priority_filter else None,
            _json.dumps(target_pdf_ids),
            len(target_pdf_ids),
            requested_by,
            discord_channel_id,
            discord_status_message_id,
        ),
    )
    get_connection().commit()
    return cur.lastrowid


def get_active_reanalyze_job() -> dict | None:
    """Return the next reanalyze job to work on, or None if nothing active.

    Order: 'processing' first (resume in-flight after a worker restart),
    then 'pending' by oldest. One job at a time — the scheduler processes
    them serially.
    """
    row = get_connection().execute(
        """SELECT * FROM reanalyze_jobs
           WHERE status IN ('processing', 'pending')
           ORDER BY (status = 'processing') DESC, created_at ASC
           LIMIT 1"""
    ).fetchone()
    return dict(row) if row else None


def get_reanalyze_job(job_id: int) -> dict | None:
    row = get_connection().execute(
        "SELECT * FROM reanalyze_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    return dict(row) if row else None


def get_recent_reanalyze_jobs(limit: int = 5) -> list[dict]:
    """Return the most recent reanalyze jobs (any status), newest first.

    Used by /status to show the active or recently-completed jobs.
    """
    rows = get_connection().execute(
        """SELECT * FROM reanalyze_jobs
           ORDER BY created_at DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def start_reanalyze_job(job_id: int) -> None:
    """Mark a pending job as processing and record started_at."""
    get_connection().execute(
        """UPDATE reanalyze_jobs
           SET status = 'processing', started_at = datetime('now')
           WHERE id = ?""",
        (job_id,),
    )
    get_connection().commit()


def update_reanalyze_job_progress(
    job_id: int,
    *,
    processed_pdf_ids: list[int] | None = None,
    failed_pdf_ids: list[int] | None = None,
    bridge_queued_pdf_ids: list[int] | None = None,
    input_tokens_delta: int = 0,
    output_tokens_delta: int = 0,
) -> None:
    """Update job progress lists + token totals. Each list parameter, if
    given, REPLACES the column value (the caller passes the full list).
    Token deltas are added incrementally.
    """
    import json as _json
    sets: list[str] = ["last_progress_at = datetime('now')"]
    args: list = []
    if processed_pdf_ids is not None:
        sets.append("processed_pdf_ids = ?")
        args.append(_json.dumps(processed_pdf_ids))
    if failed_pdf_ids is not None:
        sets.append("failed_pdf_ids = ?")
        args.append(_json.dumps(failed_pdf_ids))
    if bridge_queued_pdf_ids is not None:
        sets.append("bridge_queued_pdf_ids = ?")
        args.append(_json.dumps(bridge_queued_pdf_ids))
    if input_tokens_delta:
        sets.append("input_tokens = input_tokens + ?")
        args.append(input_tokens_delta)
    if output_tokens_delta:
        sets.append("output_tokens = output_tokens + ?")
        args.append(output_tokens_delta)
    args.append(job_id)
    get_connection().execute(
        f"UPDATE reanalyze_jobs SET {', '.join(sets)} WHERE id = ?",
        tuple(args),
    )
    get_connection().commit()


def complete_reanalyze_job(job_id: int) -> None:
    """Mark a job as complete and record completed_at."""
    get_connection().execute(
        """UPDATE reanalyze_jobs
           SET status = 'complete', completed_at = datetime('now')
           WHERE id = ?""",
        (job_id,),
    )
    get_connection().commit()


def fail_reanalyze_job(job_id: int, error_message: str) -> None:
    """Mark a job as failed with an error message."""
    get_connection().execute(
        """UPDATE reanalyze_jobs
           SET status = 'failed', completed_at = datetime('now'),
               error_message = ?
           WHERE id = ?""",
        (error_message[:500], job_id),
    )
    get_connection().commit()


# =============================================================================
# Daily pulse / synthesis history.
# =============================================================================


def get_last_daily_pulse() -> dict | None:
    """Get the full last scheduled pulse row (for cross-day context)."""
    row = get_connection().execute(
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
    rows = get_connection().execute(
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


# --- Processing log ---

def log_event(pdf_file_id: int | None, event_type: str, status: str, details: str | None = None) -> None:
    conn = get_connection()
    conn.execute(
        "INSERT INTO processing_log (pdf_file_id, event_type, status, details) VALUES (?, ?, ?, ?)",
        (pdf_file_id, event_type, status, details),
    )
    conn.commit()


# --- Stats ---

def _today_utc_range() -> tuple[str, str, str]:
    """Compute the UTC range corresponding to "today" in the configured
    display timezone (settings.timezone, default America/New_York).

    The bug we're fixing: previously this used `date.today()` which is
    SERVER-LOCAL. On Railway (UTC), `date.today()` advances at UTC
    midnight. Stats labeled "Today" would suddenly drop to 0 when UTC
    flipped over even though it's still mid-evening for the actual user.
    Fix: compute today's date in the user's configured timezone, then
    convert that local-day's [00:00, 24:00) window to UTC for the
    pdf_files.created_at comparison (which is stored as UTC).

    Returns (utc_start_str, utc_end_str, local_date_iso) — the first two
    in space-separator format that matches SQLite's `datetime('now')`,
    the third for the daily_reports.report_date lookup which is stored
    as a calendar date string in the user's local sense.
    """
    import pytz
    from config import settings
    try:
        tz = pytz.timezone(settings.timezone)
    except Exception:
        tz = pytz.UTC
    now_local = datetime.now(tz)
    local_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    local_tomorrow = local_midnight + timedelta(days=1)
    utc_start = local_midnight.astimezone(pytz.UTC).strftime("%Y-%m-%d %H:%M:%S")
    utc_end = local_tomorrow.astimezone(pytz.UTC).strftime("%Y-%m-%d %H:%M:%S")
    return utc_start, utc_end, now_local.date().isoformat()


def get_today_stats(today: str | None = None) -> dict:
    """Stats for "today" in the configured display timezone.

    `today` parameter is now ignored when the timezone-aware path is
    available (it's kept for caller-API compatibility but the actual
    comparison uses a UTC range computed from the configured TZ).
    Pass-through of a literal date string is no longer meaningful since
    the comparison is now range-based.
    """
    utc_start, utc_end, local_date = _today_utc_range()
    conn = get_connection()

    total = conn.execute(
        "SELECT COUNT(*) as c FROM pdf_files WHERE created_at >= ? AND created_at < ?",
        (utc_start, utc_end),
    ).fetchone()["c"]

    processed = conn.execute(
        "SELECT COUNT(*) as c FROM pdf_files WHERE created_at >= ? AND created_at < ? AND status = 'PROCESSED'",
        (utc_start, utc_end),
    ).fetchone()["c"]

    pending = conn.execute(
        "SELECT COUNT(*) as c FROM pdf_files WHERE created_at >= ? AND created_at < ? AND status IN ('DOWNLOADED', 'PROCESSING')",
        (utc_start, utc_end),
    ).fetchone()["c"]

    failed = conn.execute(
        "SELECT COUNT(*) as c FROM pdf_files WHERE created_at >= ? AND created_at < ? AND status = 'FAILED'",
        (utc_start, utc_end),
    ).fetchone()["c"]

    # Sum tokens across ALL analysis attempts today (re-runs cost real tokens,
    # so counting them is correct for observability).
    tokens = conn.execute(
        """SELECT COALESCE(SUM(input_tokens_used), 0) as input_t,
                  COALESCE(SUM(output_tokens_used), 0) as output_t
           FROM pdf_analyses WHERE created_at >= ? AND created_at < ?""",
        (utc_start, utc_end),
    ).fetchone()

    # Daily reports use `report_date` (calendar date string) — match the
    # local calendar date. Both routine-pulse and Gemini-pulse paths
    # store report_date in the same local-day sense.
    last_report = conn.execute(
        "SELECT report_type, discord_sent_at FROM daily_reports WHERE report_date = ? ORDER BY created_at DESC LIMIT 1",
        (local_date,),
    ).fetchone()

    return {
        "total": total,
        "processed": processed,
        "pending": pending,
        "failed": failed,
        "input_tokens": tokens["input_t"],
        "output_tokens": tokens["output_t"],
        "last_report_type": last_report["report_type"] if last_report else None,
        "last_report_sent": last_report["discord_sent_at"] if last_report else None,
    }


def get_pipeline_stats() -> dict:
    """Full-picture stats across the whole DB, not just today."""
    conn = get_connection()

    total = conn.execute("SELECT COUNT(*) as c FROM pdf_files").fetchone()["c"]

    # Status breakdown
    status_rows = conn.execute(
        "SELECT status, COUNT(*) as c FROM pdf_files GROUP BY status"
    ).fetchall()
    status_counts = {r["status"]: r["c"] for r in status_rows}

    # Priority breakdown: count each PDF once (latest analysis wins if multiple exist)
    priority_rows = conn.execute(
        """SELECT priority, COUNT(*) as c FROM (
             SELECT priority FROM pdf_analyses pa
             WHERE pa.id = (SELECT MAX(id) FROM pdf_analyses WHERE pdf_file_id = pa.pdf_file_id)
           ) GROUP BY priority"""
    ).fetchall()
    priority_counts = {r["priority"]: r["c"] for r in priority_rows}

    # Upload date range (earliest/latest PDF upload in DB)
    date_range = conn.execute(
        """SELECT MIN(dropbox_modified_at) as earliest,
                  MAX(dropbox_modified_at) as latest
           FROM pdf_files WHERE dropbox_modified_at IS NOT NULL"""
    ).fetchone()

    # Token totals (all-time)
    tokens = conn.execute(
        """SELECT COALESCE(SUM(input_tokens_used), 0) as input_t,
                  COALESCE(SUM(output_tokens_used), 0) as output_t
           FROM pdf_analyses"""
    ).fetchone()

    # Last scheduled pulse — explicitly EXCLUDE test fires.
    # Test fires are inserted with report_type='daily' (so the bridge
    # worker's flow stays uniform) but they carry a `target_channels`
    # frontmatter value that lands in report_json. Filter those out so
    # /status's "since last scheduled pulse" counter only resets at the
    # actual 13:08 UTC weekday cron firing, not at every test pulse.
    last_daily = conn.execute(
        """SELECT created_at, discord_sent_at, pdf_count
           FROM daily_reports
           WHERE report_type = 'daily'
             AND (
               report_json IS NULL
               OR json_extract(report_json, '$.target_channels') IS NULL
             )
           ORDER BY created_at DESC LIMIT 1"""
    ).fetchone()

    # Last manual pulse
    last_manual = conn.execute(
        """SELECT created_at, discord_sent_at, pdf_count
           FROM daily_reports WHERE report_type = 'manual'
           ORDER BY created_at DESC LIMIT 1"""
    ).fetchone()

    # Dropbox cursor state
    cursor_row = conn.execute("SELECT cursor, last_poll_at FROM dropbox_state WHERE id = 1").fetchone()

    # Last 5 PDFs ingested (by created_at desc)
    recent_rows = conn.execute(
        """SELECT file_name, priority, status, created_at
           FROM pdf_files
           ORDER BY created_at DESC
           LIMIT 5"""
    ).fetchall()
    recent_pdfs = [dict(r) for r in recent_rows]

    # Count uploads by Dropbox upload time (server_modified) — what actually
    # would go into a pulse, regardless of when we ingested them.
    cutoff_24h = (datetime.utcnow() - timedelta(hours=24)).isoformat()
    uploads_last_24h = conn.execute(
        "SELECT COUNT(*) as c FROM pdf_files WHERE dropbox_modified_at > ?",
        (cutoff_24h,),
    ).fetchone()["c"]

    uploads_since_last_scheduled = None
    if last_daily and last_daily["created_at"]:
        # Normalize the cutoff to match dropbox_modified_at's ISO 'T' format.
        # SQLite's datetime('now') uses space separator which lexically compares
        # less than 'T'-format strings, inflating counts without normalization.
        cutoff_pulse = _normalize_ts(last_daily["created_at"])
        row = conn.execute(
            "SELECT COUNT(*) as c FROM pdf_files WHERE dropbox_modified_at > ?",
            (cutoff_pulse,),
        ).fetchone()
        uploads_since_last_scheduled = row["c"] if row else 0

    return {
        "total_pdfs": total,
        "status_counts": status_counts,
        "priority_counts": priority_counts,
        "earliest_upload": date_range["earliest"] if date_range else None,
        "latest_upload": date_range["latest"] if date_range else None,
        "input_tokens": tokens["input_t"],
        "output_tokens": tokens["output_t"],
        "last_daily_pulse": dict(last_daily) if last_daily else None,
        "last_manual_pulse": dict(last_manual) if last_manual else None,
        "cursor_set": bool(cursor_row and cursor_row["cursor"]),
        "last_poll_at": cursor_row["last_poll_at"] if cursor_row else None,
        "recent_pdfs": recent_pdfs,
        "uploads_last_24h": uploads_last_24h,
        "uploads_since_last_scheduled": uploads_since_last_scheduled,
    }


# =============================================================================
# /ask Perplexity rate-limit log.
# =============================================================================


def count_ask_queries_today_for_user(user_id: int) -> int:
    """Count this user's /ask queries since UTC midnight."""
    today_utc_midnight = datetime.utcnow().strftime("%Y-%m-%d 00:00:00")
    row = get_connection().execute(
        "SELECT COUNT(*) AS c FROM ask_queries WHERE user_id = ? AND asked_at >= ?",
        (int(user_id), today_utc_midnight),
    ).fetchone()
    return int(row["c"]) if row else 0


def record_ask_query(user_id: int) -> None:
    conn = get_connection()
    conn.execute("INSERT INTO ask_queries (user_id) VALUES (?)", (int(user_id),))
    conn.commit()


def record_ask_bot_answer(
    asker_user_id: int,
    channel_id: int,
    question: str | None,
    answer: str,
) -> None:
    """Persist the bot's /ask reply so the next /ask from this asker in
    this channel can see it via [YOU said earlier (to this asker, cross-
    window)]: lines — the anti-recycling guard's source of truth when the
    50-msg channel.history window has scrolled past."""
    if not answer or not str(answer).strip():
        return
    conn = get_connection()
    conn.execute(
        "INSERT INTO ask_bot_answers "
        "(asker_user_id, channel_id, question, answer) VALUES (?, ?, ?, ?)",
        (
            int(asker_user_id),
            int(channel_id),
            (question or "")[:2000] or None,
            str(answer)[:4000],
        ),
    )
    conn.commit()


def get_recent_bot_answers_to_asker(
    asker_user_id: int,
    channel_id: int,
    limit: int = 5,
    max_age_days: int = 7,
) -> list[dict]:
    """Return the bot's last `limit` /ask answers to this asker in this
    channel within `max_age_days`, newest first. Returned dicts have
    keys: question, answer, answered_at.

    The recency bound (added 2026-06-10) prevents stale-answer
    injection: count-only bounding meant a quiet channel could surface
    weeks-old answers, suppressing legitimately fresh re-answers when
    circumstances had changed (prices moved, news landed). 7 days
    covers the observed recycling window (the Grand Nagus case was 30
    minutes apart) with a wide margin."""
    rows = get_connection().execute(
        "SELECT question, answer, answered_at "
        "FROM ask_bot_answers "
        "WHERE asker_user_id = ? AND channel_id = ? "
        "  AND answered_at >= datetime('now', ?) "
        "ORDER BY answered_at DESC, id DESC LIMIT ?",
        (int(asker_user_id), int(channel_id),
         f"-{int(max_age_days)} day", int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]


# =============================================================================
# Format-overhaul Phase 1: pulse_state + pulse_leans (WHAT CHANGED / TRADE BOARD)
# =============================================================================


def save_pulse_state_candidate(state_json: str, dumped_at: str) -> None:
    """Insert a context-dump state snapshot. Keeps only the last 50
    candidates (the dump job fires ~hourly; unstamped rows older than
    the working set are noise)."""
    conn = get_connection()
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
    conn = get_connection()
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
    row = get_connection().execute(
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
    conn = get_connection()
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
            conn.execute(
                "UPDATE pulse_leans SET last_seen_date = ?, "
                "context_snippet = COALESCE(?, context_snippet) WHERE id = ?",
                (today, (lean.get("context") or None), live["id"]),
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


def get_recent_user_chat_trades(
    user_id: int, days: int = 2, limit: int = 15
) -> list[dict]:
    """A user's recent chat messages that read as trade statements.

    Best-effort signal, NOT a verified ledger — these are the member's
    own words ("self-reported"). Returns [{posted_at, channel, text}],
    most recent first. Empty list on error or no matches.
    """
    try:
        cutoff = (datetime.utcnow() - timedelta(days=max(1, int(days)))).isoformat()
        rows = get_connection().execute(
            "SELECT posted_at, channel_name, content FROM chat_messages "
            "WHERE author_id = ? AND posted_at >= ? "
            "ORDER BY posted_at DESC LIMIT 300",
            (int(user_id), cutoff),
        ).fetchall()
    except Exception as e:
        log.warning(f"get_recent_user_chat_trades failed: {e}")
        return []
    out: list[dict] = []
    for r in rows:
        content = (r["content"] or "").strip()
        if not content or not _CHAT_TRADE_RE.search(content):
            continue
        out.append({
            "posted_at": (r["posted_at"] or "")[:16].replace("T", " "),
            "channel": r["channel_name"],
            "text": content[:180],
        })
        if len(out) >= limit:
            break
    return out


def get_board_leans(today: str, max_age_days: int = 5) -> list[dict]:
    """Live leans for the TRADE BOARD, newest-first. Ages out leans not
    re-affirmed within max_age_days as a side effect (keeps the board
    honest without a separate cron)."""
    conn = get_connection()
    conn.execute(
        "UPDATE pulse_leans SET status = 'aged_out' "
        "WHERE status = 'live' AND last_seen_date < date(?, ?)",
        (today, f"-{int(max_age_days)} day"),
    )
    conn.commit()
    rows = conn.execute(
        "SELECT instrument, direction, first_seen_date, last_seen_date, "
        "context_snippet FROM pulse_leans WHERE status = 'live' "
        "ORDER BY first_seen_date DESC, instrument",
    ).fetchall()
    return [dict(r) for r in rows]


# =============================================================================
# Reminder dedup (channel reminder system — reminders/job.py).
# =============================================================================


def reminder_already_sent(fire_date: str, event_id: str, lead: int) -> bool:
    """True if this (fire_date, event_id, lead) reminder already posted."""
    row = get_connection().execute(
        "SELECT 1 FROM reminder_sent WHERE fire_date = ? AND event_id = ? "
        "AND lead = ?",
        (fire_date, event_id, int(lead)),
    ).fetchone()
    return row is not None


def mark_reminder_sent(fire_date: str, event_id: str, lead: int) -> None:
    """Record that a reminder posted, so a redeploy can't double-post.
    Only called AFTER a successful Discord send."""
    from datetime import datetime
    conn = get_connection()
    conn.execute(
        "INSERT OR IGNORE INTO reminder_sent (fire_date, event_id, lead, "
        "sent_at) VALUES (?, ?, ?, ?)",
        (fire_date, event_id, int(lead), datetime.utcnow().isoformat()),
    )
    conn.commit()


# =============================================================================
# Analyst trade log (populated by analyst_log.watcher).
# =============================================================================


def analyst_trade_exists(discord_message_id: int, discord_attachment_id: int) -> bool:
    """Dedup check before OCR'ing an image we've already processed."""
    row = get_connection().execute(
        "SELECT 1 FROM analyst_trades WHERE discord_message_id = ? "
        "AND discord_attachment_id = ?",
        (int(discord_message_id), int(discord_attachment_id)),
    ).fetchone()
    return row is not None


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
    conn = get_connection()

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
            inferred_status, tracking_mode)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
    conn = get_connection()
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
    rows = get_connection().execute(
        f"""SELECT * FROM analyst_trades
           WHERE {' AND '.join(where)}
           ORDER BY posted_at DESC
           LIMIT ?""",
        (*params, limit),
    ).fetchall()
    return [dict(r) for r in rows]


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
    rows = get_connection().execute(
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
    conn = get_connection()
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


# Backwards-compat alias — existing call sites pass no caller and expect
# the legacy (Abe-only-era) global tally. Going forward, prefer
# compute_caller_win_loss_summary(caller='abe') so the intent is explicit.
def compute_abe_win_loss_summary(days: int = 30) -> dict:
    return compute_caller_win_loss_summary(days=days, caller="abe")


def format_analyst_trades_for_context(
    hours: int = 168,
    limit: int = 30,
    caller: str | None = None,
    display: str | None = None,
    tracking_mode: str | None = "caller",
    kind: str = "all",
) -> str:
    """Render the last N hours of trade-tagged rows as a context block for /ask.

    Intentionally OMITS captions and notes — we don't want the bot to quote
    the caller verbatim. The bot gets ticker/strike/expiry/action/gain only,
    and must paraphrase if the user asks "what did he say."

    When `caller` is set, restricts rows + headers to that caller. `display`
    is the human-readable name for headers (defaults to caller.title()).
    None for both = legacy global behavior (kept for backwards compat,
    but the /ask builder always passes both for hard separation).

    `kind` ∈ {"all", "recent", "open", "tally"}:
      - "all" (default) emits RECENT + OPEN + TALLY. Preserves legacy
        early-return: if no recent rows, returns "" even if positions
        or tally would otherwise emit.
      - "recent" emits only the RECENT TRADES block.
      - "open" emits only the CURRENTLY OPEN POSITIONS block.
      - "tally" emits only the W/L TALLY block.
    Any other value raises ValueError.

    Returns "" when there are no trade rows in the window AND kind="all" —
    caller can omit the block entirely.
    """
    if kind not in ("all", "recent", "open", "tally"):
        raise ValueError(
            f"kind must be one of: all, recent, open, tally; got {kind!r}"
        )

    display_name = display or (caller.title() if caller else "Abe")
    header_prefix = display_name.upper()
    out_lines: list[str] = []

    # RECENT TRADES block
    if kind in ("all", "recent"):
        rows = get_recent_analyst_trades(
            hours=hours, limit=limit, caller=caller, tracking_mode=tracking_mode,
        )
        if not rows:
            # Legacy quirk: kind='all' early-returns '' when no recent rows,
            # even if positions/tally would otherwise emit. Preserved so the
            # existing prompt-assembly call site (which checks
            # `if analyst_block:` before appending) keeps its behavior.
            # kind='recent' alone just emits no RECENT block and falls through
            # — but with no other blocks gated in (kind != all), the final
            # join is "".
            if kind == "all":
                return ""
        else:
            out_lines.append(
                f"{header_prefix}'S RECENT TRADES (last {hours // 24} days, "
                f"auto-logged from his alerts channel — for context only, "
                f"don't quote captions; he didn't share them with you):"
            )
            # Newest first per get_recent_analyst_trades; reverse so the trader
            # reads them chronologically.
            for r in reversed(rows):
                ticker = r.get("ticker") or "?"
                ct = (r.get("contract_type") or "").lower()
                ct_suffix = {"call": "C", "put": "P"}.get(ct, "")
                strike = r.get("strike")
                strike_str = (
                    f"{int(strike) if strike == int(strike) else strike}"
                    if strike is not None else "?"
                )
                expiry = r.get("expiry") or ""
                exp_short = expiry[5:] if len(expiry) >= 10 else expiry  # MM-DD
                action = (r.get("action") or "?").lower()
                # Display rule (mirrors the live announce-line rule):
                # opens/adds carry @price; closes/trims carry (±gain%).
                # 0-values treated as missing (model sentinel, not real data).
                gain = r.get("gain_pct")
                price = r.get("price")
                try:
                    price_f = float(price) if price is not None else None
                except (TypeError, ValueError):
                    price_f = None
                try:
                    gain_f = float(gain) if gain is not None else None
                except (TypeError, ValueError):
                    gain_f = None
                suffix_str = ""
                if action in ("open", "add") and price_f and price_f != 0:
                    suffix_str = f" @{price_f:.2f}"
                elif action in ("close", "trim") and gain_f is not None and gain_f != 0:
                    suffix_str = f" ({gain_f:+.1f}%)"
                posted_at = (r.get("posted_at") or "")[:16].replace("T", " ")

                # Surface inferred-status tags so the bot doesn't claim phantom
                # holdings or fabricate entries that aren't in the log.
                status_tag = ""
                status = r.get("inferred_status")
                if status == "expired_unknown":
                    if action in ("open", "add"):
                        status_tag = " [expired — no close alert]"
                    else:
                        status_tag = " [expired]"
                elif status == "close_without_open":
                    status_tag = " [exit only — no logged entry]"

                out_lines.append(
                    f"- {posted_at} — {action} {ticker} "
                    f"{strike_str}{ct_suffix} {exp_short}{suffix_str}{status_tag}"
                )

    # CURRENTLY OPEN POSITIONS block
    if kind in ("all", "open"):
        positions = get_current_analyst_positions(
            caller=caller, tracking_mode=tracking_mode,
        )
        if positions:
            if out_lines:
                out_lines.append("")
            out_lines.append(
                f"{header_prefix}'S CURRENTLY OPEN POSITIONS "
                f"(sorted by closest expiry first):"
            )
            for p in positions[:20]:
                ticker = p.get("ticker") or "?"
                ct = (p.get("contract_type") or "").lower()
                ct_suffix = {"call": "C", "put": "P"}.get(ct, "")
                strike = p.get("strike")
                strike_str = (
                    f"{int(strike) if strike == int(strike) else strike}"
                    if strike is not None else "?"
                )
                expiry = p.get("expiry") or ""
                exp_short = expiry[5:] if len(expiry) >= 10 else expiry
                # Display rule: open positions show @entry_price (the original
                # open's price), NOT the last gain pill. Gain% is a closure
                # signal — meaningless mid-flight on an open position.
                # 0-values treated as missing.
                entry_price = p.get("entry_price")
                price_str = ""
                try:
                    ep_f = float(entry_price) if entry_price is not None else None
                except (TypeError, ValueError):
                    ep_f = None
                if ep_f and ep_f != 0:
                    price_str = f" @{ep_f:.2f}"
                out_lines.append(
                    f"- {ticker} {strike_str}{ct_suffix} {exp_short}{price_str}"
                )

    # W/L TALLY block
    # Surface authoritative numbers so the bot doesn't have to recompute on
    # every "what's his win rate?" question. Convention: expirations-without-
    # close count as losses (callers rarely screenshot losers; they leak out
    # as expired open/add rows tagged `expired_unknown`).
    if kind in ("all", "tally"):
        wl = compute_caller_win_loss_summary(
            days=30, caller=caller, tracking_mode=tracking_mode,
        )
        if wl["decided"] > 0:
            if out_lines:
                out_lines.append("")
            out_lines.append(
                f"{header_prefix}'S W/L TALLY (last {wl['days']}d — "
                f"expirations-without-close counted as L):"
            )
            out_lines.append(
                f"- **{wl['wins']}W / {wl['total_losses']}L** "
                f"(documented: {wl['losses_documented']}L, "
                f"silent expiry: {wl['losses_silent_expiry']}L)"
            )
            out_lines.append(
                f"- Win rate: {wl['win_rate_pct']}% on {wl['decided']} decided trades"
            )
            if wl["avg_win_pct"] is not None:
                out_lines.append(f"- Avg win: {wl['avg_win_pct']:+.1f}%")
            if wl["avg_loss_pct"] is not None:
                out_lines.append(f"- Avg documented loss: {wl['avg_loss_pct']:+.1f}%")

            # Specific trade lists — so the bot doesn't fabricate which
            # tickers were wins vs silent-expiry losses when asked for the
            # breakdown. Each contract rendered as TICKER STRIKE(C/P) MM-DD.
            def _fmt_contract(r: dict) -> str:
                tk = r.get("ticker") or "?"
                ct = (r.get("contract_type") or "").lower()
                ct_suffix = {"call": "C", "put": "P"}.get(ct, "")
                strike = r.get("strike")
                strike_str = (
                    f"{int(strike) if strike == int(strike) else strike}"
                    if strike is not None else "?"
                )
                expiry = r.get("expiry") or ""
                exp_short = expiry[5:] if len(expiry) >= 10 else expiry
                return f"{tk} {strike_str}{ct_suffix} {exp_short}"

            if wl.get("win_trades"):
                out_lines.append("- Winning closes (specific contracts):")
                for w in wl["win_trades"][:25]:
                    gain = w.get("gain_pct")
                    gain_str = f" ({gain:+.1f}%)" if gain is not None else ""
                    out_lines.append(f"  · {_fmt_contract(w)}{gain_str}")
            if wl.get("silent_expiry_trades"):
                out_lines.append(
                    "- Silent-expiry losses (opens with no close, expired):"
                )
                for s in wl["silent_expiry_trades"][:25]:
                    out_lines.append(f"  · {_fmt_contract(s)}")

    return "\n".join(out_lines)


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
    conn = get_connection()
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


# =============================================================================
# User profiles (LLM-generated personality summaries).
# =============================================================================


def upsert_user_profile(
    *,
    user_id: int,
    username: str | None,
    display_name: str | None,
    profile_text: str,
    message_count_at_update: int,
    last_seen_message_at: str | None,
    slur_count: int = 0,
    racial_humor_score: int | None = None,
    trader_score: int | None = None,
    trader_rationale: str | None = None,
    racism_rationale: str | None = None,
    slur_examples: str | None = None,
    trader_examples: str | None = None,
    personal_ammo: str | None = None,
) -> None:
    """Insert or replace a user profile. updated_at is auto-stamped.

    Metrics (slur_count, racial_humor_score, trader_score, trader_rationale,
    slur_examples, trader_examples) are part of the profile row.
    trader_rank is NOT set here — it's computed on-read via
    get_global_trader_ranks() (the stored column is deprecated).

    slur_examples and trader_examples are JSON-encoded list[str] payloads
    (use json.dumps in the caller). Stored as TEXT to keep schema simple.
    """
    conn = get_connection()
    conn.execute(
        """INSERT INTO user_profiles
             (user_id, username, display_name, profile_text,
              message_count_at_update, last_seen_message_at,
              slur_count, racial_humor_score,
              trader_score, trader_rationale, racism_rationale,
              slur_examples, trader_examples, personal_ammo,
              updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(user_id) DO UPDATE SET
             username = excluded.username,
             display_name = excluded.display_name,
             profile_text = excluded.profile_text,
             message_count_at_update = excluded.message_count_at_update,
             last_seen_message_at = excluded.last_seen_message_at,
             slur_count = excluded.slur_count,
             racial_humor_score = COALESCE(excluded.racial_humor_score, user_profiles.racial_humor_score),
             trader_score = COALESCE(excluded.trader_score, user_profiles.trader_score),
             trader_rationale = COALESCE(excluded.trader_rationale, user_profiles.trader_rationale),
             racism_rationale = COALESCE(excluded.racism_rationale, user_profiles.racism_rationale),
             slur_examples = COALESCE(excluded.slur_examples, user_profiles.slur_examples),
             trader_examples = COALESCE(excluded.trader_examples, user_profiles.trader_examples),
             personal_ammo = COALESCE(excluded.personal_ammo, user_profiles.personal_ammo),
             updated_at = datetime('now')""",
        (
            int(user_id), username, display_name, profile_text,
            int(message_count_at_update), last_seen_message_at,
            int(slur_count), racial_humor_score,
            trader_score, trader_rationale, racism_rationale,
            slur_examples, trader_examples, personal_ammo,
        ),
    )
    conn.commit()


def get_global_trader_ranks() -> tuple[dict[int, int], int]:
    """Compute global trader rank ordering live (1 = best). Returns
    (rank_by_uid, total_ranked).

    Replaces the previously-stored user_profiles.trader_rank column.
    Now computed on-read so it can never drift from current
    trader_score values — same pattern as racism_rank in
    format_user_profiles_for_context. Cost: one SELECT across
    user_profiles (~50-100 rows) + a Python enumerate. Sub-ms.

    Tied scores break by user_id ASC (deterministic, matches the
    previous stored ordering).
    """
    rows = get_connection().execute(
        """SELECT user_id
           FROM user_profiles
           WHERE trader_score IS NOT NULL
           ORDER BY trader_score DESC, user_id ASC"""
    ).fetchall()
    rank_by_uid: dict[int, int] = {
        int(r["user_id"]): i + 1 for i, r in enumerate(rows)
    }
    return rank_by_uid, len(rows)


def recompute_trader_ranks_on_profiles() -> None:
    """DEPRECATED — no-op kept for backward compat. trader_rank is
    computed on-read via get_global_trader_ranks() now. The
    user_profiles.trader_rank column is dead storage; ignore values
    you see there from older deploys.
    """
    return  # no-op


def get_user_profile(user_id: int) -> dict | None:
    row = get_connection().execute(
        "SELECT * FROM user_profiles WHERE user_id = ?", (int(user_id),)
    ).fetchone()
    return dict(row) if row else None


def get_user_profile_by_username(username: str) -> dict | None:
    """Look up by case-insensitive username (Discord global username)."""
    row = get_connection().execute(
        "SELECT * FROM user_profiles WHERE LOWER(username) = LOWER(?)",
        (username,),
    ).fetchone()
    return dict(row) if row else None


def get_profiles_for_users(user_ids: list[int]) -> dict[int, dict]:
    """Bulk fetch profiles for a set of user_ids. Returns id-keyed dict
    so the caller can look up matching profiles without a second query."""
    if not user_ids:
        return {}
    placeholders = ",".join("?" * len(user_ids))
    rows = get_connection().execute(
        f"SELECT * FROM user_profiles WHERE user_id IN ({placeholders})",
        [int(u) for u in user_ids],
    ).fetchall()
    return {r["user_id"]: dict(r) for r in rows}


def prune_user_profiles_to_top_n(n: int) -> list[dict]:
    """Delete user_profiles rows that aren't in the top N by
    message_count_at_update. Returns the rows that were deleted so
    callers can announce / log them.

    Used by the weekly refresh after backfill upserts the newly-active
    users — anyone who dropped below the activity cutoff gets removed
    so the table stays bounded at the configured cap.

    Pass n=0 to disable (returns []).
    """
    if n <= 0:
        return []
    conn = get_connection()
    survivors = conn.execute(
        "SELECT user_id FROM user_profiles "
        "ORDER BY message_count_at_update DESC LIMIT ?",
        (int(n),),
    ).fetchall()
    survivor_ids = [r["user_id"] for r in survivors]
    if not survivor_ids:
        return []
    placeholders = ",".join("?" * len(survivor_ids))
    targets = conn.execute(
        f"""SELECT user_id, username, display_name,
                   message_count_at_update
            FROM user_profiles
            WHERE user_id NOT IN ({placeholders})""",
        survivor_ids,
    ).fetchall()
    if not targets:
        return []
    target_ids = [r["user_id"] for r in targets]
    target_placeholders = ",".join("?" * len(target_ids))
    conn.execute(
        f"DELETE FROM user_profiles WHERE user_id IN ({target_placeholders})",
        target_ids,
    )
    conn.commit()
    return [dict(r) for r in targets]


def append_ask_interaction(
    *,
    asker_display_name: str,
    asker_username: str,
    channel_name: str,
    question: str,
    answer: str,
    full_prompt: str | None = None,
    interaction_type: str = "gemini",
    tool_trace: list[dict] | None = None,
    raw_answer: str | None = None,
) -> str | None:
    """Append one /ask interaction to today's local log file. Returns the
    log file path (so a caller can later commit it to GitHub), or None on
    any write failure (logged via standard logging — non-fatal).

    Layout: one markdown file per UTC date under settings.pdf_download_dir's
    sibling `/data/ask-logs/` directory. Newest entries are appended at
    the bottom; chronological order preserved. Each entry has:

      ## <UTC timestamp>
      **Asker:** display_name (`username`) in #channel
      **Q:** <question (post-reply-resolution, what gets appended after
              the separator in the actual prompt)>
      **A:**
      <answer text>
      <details><summary>Full prompt sent to Gemini</summary>
      <full augmented user_content — profiles + analyst + chat-context +
       separator + question — exactly the string fed to Gemini>
      </details>
      ---

    `full_prompt` is optional — when None we skip the collapsible block.
    When provided, it's the literal `user_content` string built by
    `_answer_with_gemini` (after `"\n\n".join(sections)`). Gives
    forensic visibility into what the bot ACTUALLY saw — WHO'S TALKING
    profiles, [YOU said earlier]: echoes in recent chat, analyst trade
    logs, etc. — beyond just the question text.

    Completeness extensions (2026-06-10 — the QC grader was producing
    false FAILs because the log showed neither tool activity nor the
    raw model output, so tool-grounded answers looked fabricated):
      - `interaction_type`: "gemini" (full path) | "short_circuit_slur_count"
        | "short_circuit_message_count" | "quota_capped" | "failed".
        Rendered as a tag line so QC sees the FULL record, not just the
        Gemini path.
      - `tool_trace`: compact list of {tool, args, status, result_chars}
        for every tool call the model made — rendered as a TOOLS table.
      - `raw_answer`: the model's output BEFORE voice-lint cleanup /
        retries rewrote it. Only rendered (collapsed) when it differs
        from the posted answer.

    Used by the scheduler's `_ask_log_publish_job` to push the daily files
    to GitHub (pulse-data branch) for browseable QC. Doesn't write to the
    DB — pure file append. The lightweight ask_queries table still gets a
    row separately for quota tracking.
    """
    import logging as _logging
    from pathlib import Path as _Path
    from datetime import datetime, timezone
    _log = _logging.getLogger(__name__)
    try:
        now = datetime.now(timezone.utc)
        date_str = now.strftime("%Y-%m-%d")
        ts_str = now.strftime("%Y-%m-%d %H:%M:%S UTC")

        # /data/ask-logs/ — sibling of /data/pdfs, on the same Railway volume
        from config import settings as _settings
        base_dir = _Path(_settings.pdf_download_dir).resolve().parent
        log_dir = base_dir / "ask-logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{date_str}.md"

        # Header on first write of the day
        is_new = not log_path.exists()
        asker_label = asker_display_name or asker_username or "?"
        if (
            asker_username
            and asker_display_name
            and asker_display_name.lower() != asker_username.lower()
        ):
            asker_label = f"{asker_display_name} (`{asker_username}`)"

        # Truncate stupendously long Q/A to keep the daily file scannable.
        # The Q passed in here is the question text after reply/forward
        # resolution — bracketed [MESSAGE BEING REPLIED TO] block + the
        # user's typed text. 1500 chars would routinely chop the user's
        # text off — bump to match the answer side.
        def _clip(s: str, limit: int = 12000) -> str:
            s = (s or "").strip()
            return s if len(s) <= limit else s[:limit] + "\n\n_…(truncated)_"

        # full_prompt is the FULL user_content sent to Gemini. Cap higher
        # (40k) so profiles + recent chat + analyst blocks all survive.
        # Above that we tail-truncate; the missing tail is almost always
        # historical recent-chat which is the least valuable forensically.
        def _clip_prompt(s: str, limit: int = 40000) -> str:
            s = (s or "").strip()
            if len(s) <= limit:
                return s
            return s[:limit] + "\n\n_…(prompt truncated for log readability)_"

        # Markdown collapsible <details> block — GitHub + most viewers
        # render the summary and hide the body until clicked. Keeps the
        # log skimmable while preserving full forensic fidelity. The
        # fenced code block inside uses ```text to suppress markdown
        # interpretation of any [tags] / **emphasis** inside the prompt.
        if full_prompt:
            prompt_section = (
                "<details>\n"
                f"<summary>📋 Full prompt sent to Gemini "
                f"({len(full_prompt):,} chars — profiles + analyst + "
                f"recent chat + question)</summary>\n\n"
                "```text\n"
                f"{_clip_prompt(full_prompt)}\n"
                "```\n"
                "</details>\n\n"
            )
        else:
            prompt_section = ""

        # Interaction-type tag — only rendered for non-default types so
        # existing Gemini-path entries keep their familiar shape.
        type_line = (
            f"**Type:** `{interaction_type}`\n\n"
            if interaction_type and interaction_type != "gemini" else ""
        )

        # Tool trace table — one row per tool call the model made.
        tools_section = ""
        if tool_trace:
            rows = []
            for t in tool_trace[:12]:
                args_s = ", ".join(
                    f"{k}={v}" for k, v in (t.get("args") or {}).items()
                )[:120]
                rows.append(
                    f"| {t.get('tool', '?')} | {args_s or '—'} | "
                    f"{t.get('status', '?')} | "
                    f"{t.get('result_chars', '?')} |"
                )
            tools_section = (
                "**Tools called:**\n\n"
                "| tool | args | status | result chars |\n"
                "|---|---|---|---|\n"
                + "\n".join(rows) + "\n\n"
            )

        # Raw-answer block — only when cleanup/retries actually changed
        # the output, so the log shows ground truth without doubling
        # every entry.
        raw_section = ""
        if raw_answer and raw_answer.strip() and \
                raw_answer.strip() != (answer or "").strip():
            raw_section = (
                "<details>\n"
                "<summary>🔧 Raw model output (before voice-lint / "
                "retry rewrites)</summary>\n\n"
                "```text\n"
                f"{_clip(raw_answer, 8000)}\n"
                "```\n"
                "</details>\n\n"
            )

        entry = (
            (f"# /ask interactions — {date_str}\n\n" if is_new else "")
            + f"## {ts_str}\n\n"
            f"**Asker:** {asker_label} in #{channel_name or '(unknown)'}\n\n"
            f"{type_line}"
            f"**Q:** {_clip(question)}\n\n"
            "**A:**\n\n"
            f"{_clip(answer)}\n\n"
            f"{tools_section}"
            f"{raw_section}"
            f"{prompt_section}"
            "---\n\n"
        )
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(entry)
        return str(log_path)
    except Exception as e:
        _log.warning(f"append_ask_interaction failed (non-fatal): {e}")
        return None


def export_user_profiles_markdown() -> str:
    """Render every row in user_profiles as a single markdown document.

    Format: header with generation timestamp + profile count, then one
    section per user (sorted by message count desc) with display name,
    username, msg count, last-seen timestamp, and the full profile text.

    Used by the daily refresh job to publish a snapshot to GitHub
    (pulse-data branch) so users can read the current dossier set
    without shell access to /data/reports.db.
    """
    from datetime import datetime, timezone
    import json as _json
    rows = get_connection().execute(
        """SELECT user_id, display_name, username, message_count_at_update,
                  last_seen_message_at, datetime(updated_at) AS updated_at,
                  profile_text,
                  slur_count, racial_humor_score,
                  trader_score, trader_rationale, racism_rationale,
                  slur_examples
           FROM user_profiles
           ORDER BY message_count_at_update DESC"""
    ).fetchall()

    # Compute trader_rank on-read across all profiled users — replaces
    # the previously-stored trader_rank column. See
    # get_global_trader_ranks() docstring for the deprecation note.
    trader_rank_by_uid, trader_rank_total = get_global_trader_ranks()

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines: list[str] = [
        "# User profiles snapshot",
        "",
        f"_Auto-generated by the daily user-profile refresh job._",
        f"_Generated at: **{now}**_  ",
        f"_Total profiles: **{len(rows)}**_",
        "",
        "Profiles are sorted by message count (most active first). Each "
        "profile is regenerated daily by `scripts/backfill_user_profiles.py` "
        "when the user has accumulated `profile_delta_threshold` new "
        "messages since the last refresh.",
        "",
        "---",
        "",
    ]

    for r in rows:
        dn = r["display_name"] or r["username"] or "?"
        uname = r["username"] or ""
        msg_count = r["message_count_at_update"] or 0
        last_seen = (r["last_seen_message_at"] or "")[:19].replace("T", " ")
        updated = r["updated_at"] or ""
        body = (r["profile_text"] or "").strip()
        slur_n = r["slur_count"] or 0
        rh = r["racial_humor_score"]
        ts = r["trader_score"]
        tr_rank = trader_rank_by_uid.get(int(r["user_id"]))
        tr_rationale = (r["trader_rationale"] or "").strip()
        racism_rationale_str = (r["racism_rationale"] or "").strip()
        try:
            slur_examples_list = _json.loads(r["slur_examples"] or "[]")
        except Exception:
            slur_examples_list = []
        # personal_ammo + trader_examples columns no longer rendered.
        # Their content lives inside profile_text now (5 sections).
        # Stale DB data in those columns is ignored by all consumers.

        header = f"## {dn}"
        if uname and uname.lower() != dn.lower():
            header += f" (`{uname}`)"
        lines.append(header)
        lines.append("")
        # Identity + activity line. user_id is the canonical Discord
        # snowflake — surfaced here so a reader can disambiguate
        # display-name collisions (e.g. multiple "kloh" variants) and
        # so /ask mentions resolve cleanly back to the right profile.
        uid_str = str(r["user_id"]) if r["user_id"] else "?"
        lines.append(
            f"_user_id: `{uid_str}` · {msg_count:,} msgs · "
            f"last activity {last_seen} · refreshed {updated} UTC_"
        )
        lines.append("")

        # Scores block — surfaces the hidden hierarchy metrics that the
        # /ask bot uses internally for clapback context. Reading this
        # publicly is fine; the bot just doesn't quote raw numbers in
        # answers (it uses ordinal ranks).
        # ONE race score shown — racial_humor_score is the canonical
        # number. The regex-based slur_count is still tracked in the DB
        # (deterministic floor signal) but no longer surfaced as a
        # separate display value — collapses two confusing numbers into
        # the one calibrated 0-100 score.
        score_bits: list[str] = []
        if rh is not None:
            score_bits.append(f"**racial-humor:** {rh}/100")
        if ts is not None:
            score_bits.append(f"**trader-score:** {ts}/100")
        if tr_rank is not None:
            score_bits.append(f"**trader-rank:** #{tr_rank}/{trader_rank_total}")
        if score_bits:
            lines.append(f"> {' · '.join(score_bits)}")
        if tr_rationale:
            lines.append(f"> _trader: {tr_rationale}_")
        if racism_rationale_str:
            lines.append(f"> _racism: {racism_rationale_str}_")
        # Profile body now carries all the ammo content inline in
        # named sections (Voice / Retarded takes / Recent trades /
        # Recent personal life). For OLD profiles that haven't been
        # re-rendered under the 5-section structure yet, surface
        # the regex slur block as a fallback only.
        if slur_examples_list and "**Voice" not in (body or ""):
            lines.append(">")
            lines.append("> **Recent slur usage (regex fallback):**")
            for ex in slur_examples_list[:3]:
                snippet = (ex or "")[:140].replace("\n", " ").strip()
                if snippet:
                    lines.append(f"> - {snippet}")
        lines.append("")

        lines.append(body)
        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def find_users_mentioned_in_text(text: str) -> list[int]:
    """Return user_ids of profiled users mentioned in `text`. Catches:
      - Discord @-mentions: `<@123>`, `<@!123>`
      - Substring/word-boundary matches against username + display_name
        for any user with a profile in the table.

    Names shorter than 3 chars are ignored to avoid spurious matches
    (someone named "Al" shouldn't get pulled in by every mention of
    "all" or "also"). Returns deduplicated list.
    """
    import re
    if not text:
        return []
    matches: set[int] = set()
    # Discord-encoded @-mentions
    for m in re.finditer(r"<@!?(\d+)>", text):
        try:
            matches.add(int(m.group(1)))
        except ValueError:
            continue
    # Name-based fuzzy match (word boundaries)
    rows = get_connection().execute(
        "SELECT user_id, username, display_name FROM user_profiles"
    ).fetchall()
    text_lower = text.lower()
    # Build the needle set per user: full username, full display_name,
    # AND individual tokens from each (whitespace-split + punctuation-strip).
    # The token expansion is what catches first-name-only references like
    # "Zach" matching a display_name of "Zach M." or "Zachary T." — the
    # previous full-needle-only match missed those entirely and produced
    # unloaded-subject answers (hallucination risk per the audit).
    def _split_tokens(s: str) -> list[str]:
        """Split into alphanumeric tokens, preserving case (for the
        short-distinctive uppercase pass). Caller lowercases when needed."""
        cleaned = re.sub(r"[^a-zA-Z0-9_]+", " ", s)
        return [t for t in cleaned.split() if t]

    # Tokenize the INPUT TEXT once. Used for two passes:
    #   1) Prefix matching ("zach" in input vs "zachary" in profile)
    #   2) Implicit — the standard word-boundary scan still operates on the
    #      raw text_lower, so input tokens aren't consulted there.
    input_tokens_lower: set[str] = set()
    for m in re.finditer(r"[A-Za-z0-9_]+", text):
        tok = m.group(0).lower()
        if len(tok) >= 4:
            input_tokens_lower.add(tok)

    # Drop very-generic tokens that would false-positive in normal prose.
    # (Adjust if a real username collides with a stop word.)
    STOP = {
        "the", "and", "you", "for", "with", "this", "that",
        "are", "was", "but", "not", "all", "any", "did", "has",
        "her", "his", "him", "she", "out", "now", "ask",
        "give", "what", "why", "how", "who", "post", "posts",
        "feel", "feels", "think", "thought", "thinks", "have",
        "from", "they", "them", "their", "been", "were", "would",
        "could", "should", "about", "into", "than", "then", "when",
        "where", "which", "while", "your", "our", "ours", "mine",
        "just", "like", "want", "need", "make", "made", "take", "took",
        "good", "bad", "great", "yeah", "nah", "lol", "lmao",
    }

    for r in rows:
        user_id = r["user_id"]
        # Build needles in three buckets:
        #   - lowercase tokens (≥3 chars) for the word-boundary scan
        #   - lowercase tokens (≥5 chars) eligible as prefix-match TARGETS
        #     of input tokens (≥4 chars), so "zach" finds "zachary"
        #   - short distinctive uppercase ALL-CAPS names (2-3 chars) like
        #     "BK", "RJ" — these get a case-sensitive scan against the
        #     ORIGINAL text so "bike" doesn't false-match "BK"
        profile_tokens_lower: set[str] = set()
        short_distinctive: set[str] = set()
        for raw in (r["username"], r["display_name"]):
            if not raw:
                continue
            raw_l = raw.lower()
            if len(raw_l) >= 3:
                profile_tokens_lower.add(raw_l)
            if 2 <= len(raw) <= 3 and raw.isalpha() and raw.isupper():
                short_distinctive.add(raw)
            for tok in _split_tokens(raw):
                if len(tok) >= 3:
                    profile_tokens_lower.add(tok.lower())
                if 2 <= len(tok) <= 3 and tok.isalpha() and tok.isupper():
                    short_distinctive.add(tok)
        profile_tokens_lower -= STOP

        matched = False
        # Pass 1: standard word-boundary match against text_lower.
        for needle in profile_tokens_lower:
            if re.search(rf"\b{re.escape(needle)}\b", text_lower):
                matches.add(user_id)
                matched = True
                break
        if matched:
            continue

        # Pass 2: prefix match — an input token (≥4 chars, e.g. "zach")
        # is the prefix of a profile token (≥5 chars, e.g. "zachary").
        # Constrained to ≥4/≥5 to avoid "any" matching "anything".
        for inp in input_tokens_lower:
            if inp in STOP:
                continue
            for prof in profile_tokens_lower:
                if len(prof) >= 5 and prof.startswith(inp) and prof != inp:
                    matches.add(user_id)
                    matched = True
                    break
            if matched:
                break
        if matched:
            continue

        # Pass 3: short distinctive ALL-CAPS names (case-sensitive against
        # the original text). Catches "BK" without firing on "bike".
        for needle in short_distinctive:
            if re.search(rf"\b{re.escape(needle)}\b", text):
                matches.add(user_id)
                break
    return list(matches)


_PROFILES_BLOCK_BUDGET_CHARS = 18000  # cap WHO'S TALKING block
_PROFILES_BLOCK_PER_USER_CHARS = 2500  # cap each individual profile
_PROFILES_BLOCK_MAX_USERS = 15         # hard ceiling on user count


def format_user_profiles_for_context(
    user_ids: list[int],
    *,
    max_chars: int = _PROFILES_BLOCK_BUDGET_CHARS,
    max_users: int = _PROFILES_BLOCK_MAX_USERS,
) -> str:
    """Render a "WHO'S TALKING" block for the given user_ids. Skips users
    with no profile (lurkers, new joiners). Returns "" when nobody on the
    list has been profiled.

    Budget-aware (fix #4): caps total block at `max_chars` (~18KB) and
    user count at `max_users` (15). When the candidate list exceeds
    either, profiles are prioritized by message_count_at_update DESC
    (most-active members first) so the heaviest yappers — who are most
    likely the subjects/askers — never get cut. Low-activity profiles
    drop from the tail when budget gets tight.

    Header: `- **DisplayName** (username, <@user_id>): <metrics>: <text>`.
    Metrics inline (private hierarchies): racism-rank (combined slur +
    racial-humor signal) among this conv + global trader rank with
    one-line rationale. Bot uses these ONLY for comparative answers —
    never enumerated or quoted as raw numbers.

    Also injects up to 3 slur_examples and 3 trader_examples per user
    so the bot has actual recent quotes/moments to draw on for Type 3
    clapbacks and trader-rank discussions, not just the prose profile.
    """
    import json as _json
    profiles = get_profiles_for_users(user_ids)
    if not profiles:
        return ""

    # Fix #6: racism rank uses ONLY racial_humor_score (LLM-judged, 0-100
    # calibrated). The previous formula summed regex slur_count + this
    # score, but the regex count's magnitude was either dwarfed by the
    # LLM score (humor 75 + slurs 5 = 80, score dominates) or wildly
    # outweighed it for heavy literal-slur users (humor 50 + slurs 250 =
    # 300, count dominates) — the sum was unstable and the units weren't
    # comparable. racial_humor_score already INCLUDES literal slurs in
    # its calibration brackets, so the regex count was double-counting
    # the same signal anyway. Single source of truth now.
    by_racism = sorted(
        [
            (uid, int(p.get("racial_humor_score") or 0))
            for uid, p in profiles.items()
            if (p.get("racial_humor_score") or 0) > 0
        ],
        key=lambda t: (-t[1], t[0]),
    )
    racism_rank_by_uid: dict[int, int] = {uid: i + 1 for i, (uid, _) in enumerate(by_racism)}
    racism_total_in_conv = len(by_racism)

    # trader_rank — GLOBAL ordering across ALL profiled users (not
    # scoped to this conversation). Computed on-read via
    # get_global_trader_ranks() — see that function's docstring for
    # the deprecation note on the stored trader_rank column.
    trader_rank_by_uid, trader_rank_total = get_global_trader_ranks()

    # Budget enforcement (fix #4): prioritize most-active members so the
    # heaviest yappers — most likely subjects/askers — never get cut.
    # Tail-truncate when total budget exceeded.
    profile_items = sorted(
        profiles.items(),
        key=lambda kv: -(kv[1].get("message_count_at_update") or 0),
    )[:int(max_users)]

    lines = [
        "WHO'S TALKING (background on people active in this conversation):",
    ]
    running_chars = len(lines[0])
    truncated = 0
    for uid, p in profile_items:
        dn = p.get("display_name") or p.get("username") or f"user_{uid}"
        uname = p.get("username") or ""
        # No per-profile truncation. The total-block budget below
        # (running_chars > max_chars → omit this profile) provides
        # the only cap. New 5-section profiles average 3000-3500
        # chars; the previous 2500-char per-profile clip was
        # silently dropping the Recent personal life section.
        # With WHO'S TALKING scoped to asker + mentions + reply/
        # forward authors (typically 1-3 profiles), the 18K budget
        # comfortably fits full profile_text for all of them.
        text = (p.get("profile_text") or "").strip()
        mention = f"<@{uid}>"
        if uname and uname.lower() != dn.lower():
            ident = f"**{dn}** ({uname}, {mention})"
        else:
            ident = f"**{dn}** ({mention})"

        # Private metrics inline — surfaced as ordinal ranks only.
        # racism-rank exposes both signals (humor + literal) so the bot
        # can answer "who's worst" vs "who actually uses slurs" if asked.
        metric_bits: list[str] = []
        rr = racism_rank_by_uid.get(uid)
        humor = p.get("racial_humor_score")
        slurs = int(p.get("slur_count") or 0)
        racism_rationale = (p.get("racism_rationale") or "").strip()
        sub_signal = []
        if humor is not None:
            sub_signal.append(f"humor:{humor}/100")
        if slurs > 0:
            sub_signal.append(f"slurs:{slurs}")
        sub = f" ({', '.join(sub_signal)})" if sub_signal else ""
        if rr and racism_total_in_conv >= 3:
            # Make the SCOPE unmistakable — this ranks only the people
            # active in THIS conversation, not the global leaderboard.
            # The bot conflated the two (2026-06-24: told sunny "you're
            # #1" off a conv-scoped rank while the global top-5 had him
            # absent). Leaderboard claims must use lookup_user_profile.
            base = (f"racism-rank #{rr} of {racism_total_in_conv} ACTIVE "
                    f"here (conversation-scoped, NOT the global "
                    f"leaderboard){sub}")
            if racism_rationale:
                metric_bits.append(f"{base} — {racism_rationale}")
            else:
                metric_bits.append(base)
        elif rr:
            # Denominator < 3: "#1 of 1" is a meaningless ordinal the bot
            # has mis-cited as a global "#1". Show the raw signal, not a
            # rank — the global leaderboard is the tool's job.
            base = (f"racism signal{sub} — too few active here to rank "
                    f"(global leaderboard via lookup_user_profile)")
            if racism_rationale:
                metric_bits.append(f"{base} — {racism_rationale}")
            else:
                metric_bits.append(base)
        else:
            metric_bits.append(f"racism-rank: not in this conv's top{sub}")
        # trader_rank — computed on-read from current trader_score
        # values, not the (now-deprecated) stored column. Includes
        # rank/total for the answer like "you're #7 of 32 profiled."
        tr = trader_rank_by_uid.get(uid)
        ts_rationale = p.get("trader_rationale")
        if tr:
            base = f"trader-rank #{tr}/{trader_rank_total}"
            if ts_rationale:
                metric_bits.append(f"{base} ({ts_rationale})")
            else:
                metric_bits.append(base)
        else:
            metric_bits.append("trader-rank: not scored")
        metrics_line = " · ".join(metric_bits)

        # Examples surface is now profile_text itself — the Voice,
        # Retarded takes, Recent trades, and Recent personal life
        # sections inside the markdown body carry all the ammo the
        # bot needs. Old slur_examples regex extraction stays as a
        # deterministic fallback for profiles that haven't been
        # re-run under the 5-section structure yet.
        try:
            slur_ex_list = _json.loads(p.get("slur_examples") or "[]")
        except Exception:
            slur_ex_list = []

        examples_section = ""
        # Only show the regex slur examples as a small inline block
        # IF the profile_text doesn't already contain a Voice section
        # (old-format profiles). The body of the profile carries
        # everything else.
        if slur_ex_list and "**Voice" not in (text or ""):
            ex_lines = ["  recent slur usage (regex fallback):"]
            for ex in slur_ex_list[:3]:
                snippet = (ex or "")[:140].replace("\n", " ").strip()
                if snippet:
                    ex_lines.append(f"    · {snippet}")
            examples_section = "\n" + "\n".join(ex_lines)

        rendered = (
            f"- {ident} — _{metrics_line}_:{examples_section}\n{text}\n"
        )
        if running_chars + len(rendered) > int(max_chars):
            truncated += 1
            continue
        lines.append(rendered)
        running_chars += len(rendered) + 1  # +1 for newline join
    if truncated > 0:
        lines.append(
            f"_(...{truncated} additional profile(s) omitted to fit context budget)_"
        )
    return "\n".join(lines)


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
        row = get_connection().execute(sql, tuple(params)).fetchone()
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
    row = get_connection().execute(
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


def store_chat_message(
    *,
    discord_message_id: int,
    channel_id: int,
    channel_name: str,
    author_id: int,
    author_username: str | None,
    author_display: str | None,
    content: str | None,
    posted_at: str,
    has_attachments: bool = False,
    attachment_urls: str | None = None,
    embed_texts: str | None = None,
    reply_parent_id: int | None = None,
) -> bool:
    """INSERT a row into chat_messages, silently skipping duplicates via
    the UNIQUE constraint on discord_message_id. Returns True if a new
    row was written, False if it was a duplicate (caller can use this
    return value to count new ingestions during catch-up).

    `attachment_urls` and `embed_texts` are pre-JSON-encoded strings (or
    None) — caller does the encoding so this helper stays thin.
    """
    conn = get_connection()
    try:
        cur = conn.execute(
            """INSERT OR IGNORE INTO chat_messages
                 (discord_message_id, channel_id, channel_name,
                  author_id, author_username, author_display,
                  content, posted_at,
                  has_attachments, attachment_urls, embed_texts,
                  reply_parent_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                int(discord_message_id), int(channel_id), channel_name,
                int(author_id), author_username, author_display,
                content, posted_at,
                1 if has_attachments else 0, attachment_urls, embed_texts,
                int(reply_parent_id) if reply_parent_id else None,
            ),
        )
        conn.commit()
        return cur.rowcount > 0
    except Exception:
        # Schema problems, encoding edge cases, etc. — never raise to the
        # caller; chat ingestion is best-effort.
        return False


def record_pipeline_event(
    event_type: str,
    status: str,
    payload: dict | None = None,
) -> None:
    """Append a row to pipeline_events. Best-effort: failures here are
    swallowed since this is observability, never the critical path.

    Common event types:
      - 'profile_refresh' — daily user-profile job summary
      - 'chat_catchup'    — chat_messages scan summary
    Status: 'completed', 'failed', 'partial'.
    Payload: any JSON-serializable dict of run stats.
    """
    import json as _json
    try:
        conn = get_connection()
        conn.execute(
            "INSERT INTO pipeline_events (event_type, status, payload) "
            "VALUES (?, ?, ?)",
            (
                str(event_type)[:64],
                str(status)[:32],
                _json.dumps(payload or {}, default=str)[:8000],
            ),
        )
        conn.commit()
    except Exception:
        # Observability must never break the calling job
        pass


def get_recent_pipeline_events(
    event_type: str | None = None,
    *,
    limit: int = 20,
) -> list[dict]:
    """Read recent pipeline events for a /status command or
    historical trend analysis. Newest-first.
    """
    if event_type:
        rows = get_connection().execute(
            """SELECT id, event_type, status, payload,
                      datetime(created_at) AS created_at
               FROM pipeline_events
               WHERE event_type = ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (event_type, int(limit)),
        ).fetchall()
    else:
        rows = get_connection().execute(
            """SELECT id, event_type, status, payload,
                      datetime(created_at) AS created_at
               FROM pipeline_events
               ORDER BY created_at DESC
               LIMIT ?""",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


def find_oldest_chat_gap(
    channel_id: int,
    *,
    days: int = 30,
    gap_minutes: int = 60,
) -> str | None:
    """Find the earliest gap in stored chat_messages for a channel,
    within the last `days`. A "gap" is any stretch >= `gap_minutes`
    between consecutive stored messages.

    Returns the timestamp of the LAST message before the oldest such
    gap (so the catchup can resume scanning from there forward to fill
    it). Returns None when no gaps are found — in that case the caller
    should fall back to the MAX-based resume.

    This is the structural fix for the bug we hit: live ingestion
    advanced MAX(posted_at) to today after a partial catchup, hiding
    a historical gap from the simple latest-buffer resume. Gap-detect
    finds the hole regardless of what's been written since.
    """
    from datetime import datetime, timedelta, timezone
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=int(days))
    ).isoformat()
    row = get_connection().execute(
        """WITH ordered AS (
               SELECT posted_at,
                      LAG(posted_at) OVER (ORDER BY posted_at) AS prev_at
               FROM chat_messages
               WHERE channel_id = ? AND posted_at >= ?
           )
           SELECT MIN(prev_at) AS gap_start
           FROM ordered
           WHERE prev_at IS NOT NULL
             AND (julianday(posted_at) - julianday(prev_at)) * 24 * 60 >= ?""",
        (int(channel_id), cutoff, int(gap_minutes)),
    ).fetchone()
    return row[0] if row and row[0] else None


def count_chat_messages_for_channels(
    channel_names: list[str] | None,
    *,
    days: int = 30,
) -> int:
    """Quick coverage check: how many chat_messages rows exist within
    the last `days`, optionally scoped to specific channels.

    `channel_names`:
      - None or empty list → count across ALL ingested channels
      - non-empty list → scope to those channels

    Used by the profile-refresh pipeline to decide whether the local
    store has enough data to skip the Discord scan.
    """
    from datetime import datetime, timedelta, timezone
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=int(days))
    ).isoformat()
    if channel_names:
        placeholders = ",".join("?" for _ in channel_names)
        row = get_connection().execute(
            f"""SELECT COUNT(*) FROM chat_messages
                WHERE channel_name IN ({placeholders})
                  AND posted_at >= ?""",
            (*channel_names, cutoff),
        ).fetchone()
    else:
        row = get_connection().execute(
            """SELECT COUNT(*) FROM chat_messages
               WHERE posted_at >= ?""",
            (cutoff,),
        ).fetchone()
    return int(row[0]) if row else 0


def load_chat_messages_for_profiles(
    channel_names: list[str] | None = None,
    *,
    days: int = 30,
) -> list[dict]:
    """Load every non-empty chat_messages row within the last `days`,
    ordered oldest-first. Returned dicts carry the keys the
    profile-refresh pipeline needs:

      author_id, author_username, author_display, channel_name,
      content, posted_at, attachment_urls, embed_texts,
      image_ocr_text

    `channel_names`:
      - None or empty list → no channel filter, load EVERY ingested
        channel. This is the default since the profile builder switched
        to "use all chat_messages content."
      - non-empty list → restrict to those channel names (legacy path,
        kept for ad-hoc backfill runs that want to scope tighter).

    image_ocr_text is included so screenshot content (especially from
    eager-OCR channels like gain-loss-porn) contributes to the profile
    signal alongside text and embeds.

    This replaces the Discord-history scan in
    scripts/backfill_user_profiles.py with a single SQL query —
    cheaper, faster, immune to gateway flaps + twin-client contention.
    """
    from datetime import datetime, timedelta, timezone
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=int(days))
    ).isoformat()
    if channel_names:
        placeholders = ",".join("?" for _ in channel_names)
        rows = get_connection().execute(
            f"""SELECT author_id, author_username, author_display,
                       channel_name, content, posted_at,
                       attachment_urls, embed_texts, image_ocr_text
                FROM chat_messages
                WHERE channel_name IN ({placeholders})
                  AND posted_at >= ?
                ORDER BY posted_at ASC""",
            (*channel_names, cutoff),
        ).fetchall()
    else:
        rows = get_connection().execute(
            """SELECT author_id, author_username, author_display,
                      channel_name, content, posted_at,
                      attachment_urls, embed_texts, image_ocr_text
               FROM chat_messages
               WHERE posted_at >= ?
               ORDER BY posted_at ASC""",
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


def purge_old_chat_messages(days: int) -> int:
    """Delete chat_messages older than `days`. Returns count of deleted
    rows. Used by the daily retention cron — table stays bounded.
    Called with days=0 means "delete nothing"; we guard at the entry
    to avoid wiping the table by accident.
    """
    if days <= 0:
        return 0
    conn = get_connection()
    cur = conn.execute(
        "DELETE FROM chat_messages WHERE posted_at < datetime('now', ?)",
        (f"-{int(days)} days",),
    )
    conn.commit()
    return cur.rowcount


def run_retention_purge(
    *,
    pdf_analyses_keep_per_pdf: int = 2,
    processing_log_days: int = 30,
    pipeline_events_days: int = 90,
    manual_reports_days: int = 30,
) -> dict[str, int]:
    """Trim append-only tables. Returns {table_name: rows_deleted}.

    Single transaction per table — failures on one don't roll back
    the others. Safe to run while the bot is up: each DELETE uses
    indexed criteria and SQLite's per-statement locking releases
    quickly.

    pdf_analyses: keep latest `pdf_analyses_keep_per_pdf` per
        pdf_file_id (default 2 — current + immediate prior). The
        MAX(id) GROUP BY semantics that synthesis queries rely on
        stays correct because we keep the highest-id rows.
    processing_log: drop > `processing_log_days` days old (default 30).
    pipeline_events: drop > `pipeline_events_days` days old
        (default 90 — covers two QC cycles of history).
    manual_reports_days: drop daily_reports with report_type='manual'
        older than `manual_reports_days` days (default 30). Manual
        /pulse invocations are tests, not production records;
        report_type='daily' stays forever (small volume).
    """
    conn = get_connection()
    results: dict[str, int] = {}

    # pdf_analyses — keep latest N per pdf_file_id
    try:
        cur = conn.execute(
            """DELETE FROM pdf_analyses
                WHERE id NOT IN (
                    SELECT id FROM (
                        SELECT id,
                               ROW_NUMBER() OVER (
                                   PARTITION BY pdf_file_id
                                   ORDER BY id DESC
                               ) AS rn
                          FROM pdf_analyses
                    )
                    WHERE rn <= ?
                )""",
            (int(pdf_analyses_keep_per_pdf),),
        )
        conn.commit()
        results["pdf_analyses"] = cur.rowcount
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            f"retention purge pdf_analyses failed: {e}"
        )
        results["pdf_analyses"] = -1

    # processing_log — N-day retention
    try:
        cur = conn.execute(
            "DELETE FROM processing_log WHERE created_at < datetime('now', ?)",
            (f"-{int(processing_log_days)} days",),
        )
        conn.commit()
        results["processing_log"] = cur.rowcount
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            f"retention purge processing_log failed: {e}"
        )
        results["processing_log"] = -1

    # pipeline_events — N-day retention
    try:
        cur = conn.execute(
            "DELETE FROM pipeline_events WHERE created_at < datetime('now', ?)",
            (f"-{int(pipeline_events_days)} days",),
        )
        conn.commit()
        results["pipeline_events"] = cur.rowcount
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            f"retention purge pipeline_events failed: {e}"
        )
        results["pipeline_events"] = -1

    # daily_reports — manual-only N-day retention; daily_reports of
    # report_type='daily' are the production record, kept forever.
    try:
        cur = conn.execute(
            """DELETE FROM daily_reports
                WHERE report_type = 'manual'
                  AND created_at < datetime('now', ?)""",
            (f"-{int(manual_reports_days)} days",),
        )
        conn.commit()
        results["daily_reports_manual"] = cur.rowcount
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            f"retention purge daily_reports_manual failed: {e}"
        )
        results["daily_reports_manual"] = -1

    return results


def get_chat_message_row(discord_message_id: int) -> dict | None:
    """Fetch a single chat_messages row by Discord message ID. Used by
    the OCR helper to read the URL list + check the cached OCR text
    before deciding whether to call Gemini.
    """
    row = get_connection().execute(
        """SELECT id, discord_message_id, channel_id, channel_name,
                  author_id, author_username, author_display, content,
                  posted_at, has_attachments, attachment_urls,
                  embed_texts, reply_parent_id,
                  image_ocr_text, image_ocr_status, image_ocr_at
             FROM chat_messages
            WHERE discord_message_id = ?""",
        (int(discord_message_id),),
    ).fetchone()
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def set_chat_image_ocr(
    discord_message_id: int,
    *,
    ocr_text: str | None,
    status: str,
) -> bool:
    """Write OCR result back to chat_messages. `status` is one of:
      - 'success'    — ocr_text contains the extracted content
      - 'no_images'  — message has attachments but none were images
      - 'failed'     — OCR call errored or returned empty (cache the
                       failure so we don't retry on every /ask)
    Returns True if a row was updated, False if no matching row.
    """
    conn = get_connection()
    cur = conn.execute(
        """UPDATE chat_messages
              SET image_ocr_text = ?,
                  image_ocr_status = ?,
                  image_ocr_at = datetime('now')
            WHERE discord_message_id = ?""",
        (ocr_text, status, int(discord_message_id)),
    )
    conn.commit()
    return cur.rowcount > 0


def get_latest_chat_message_posted_at(channel_id: int | None = None) -> str | None:
    """Most-recent posted_at across stored chat. Optionally scoped to a
    channel — used by the catch-up pass to know how far back to scan.
    Returns ISO timestamp string or None if no rows.
    """
    if channel_id is not None:
        row = get_connection().execute(
            "SELECT MAX(posted_at) FROM chat_messages WHERE channel_id = ?",
            (int(channel_id),),
        ).fetchone()
    else:
        row = get_connection().execute(
            "SELECT MAX(posted_at) FROM chat_messages"
        ).fetchone()
    return row[0] if row and row[0] else None


def lookup_user_ranks(
    *,
    username: str | None = None,
    metric: str | None = None,
    rank_position: int | None = None,
    top_n: int = 5,
    from_bottom: bool = False,
) -> dict:
    """Look up rank info. Three modes (use exactly one):

      1. `username` set → return that user's trader_rank, racism_rank,
         and both rationales.

    `from_bottom` (rank_position mode only): when True, rank_position=1
    returns the WORST-ranked user (lowest trader_score / lowest
    racial_humor_score above 0), =2 returns second-worst, etc. Used to
    answer "who's the worst trader" without exposing the score ordering
    to the asker. The returned `rank` field reflects the user's actual
    position FROM THE TOP (so the bot still says "rank #49/49") — only
    the lookup direction differs.
      2. `metric` + `rank_position` set → return the ONE user at that
         rank position (no cap on N — supports "who's #50" too).
      3. `username` unset, no rank_position, `metric` in {"trader",
         "racism"} → return the top `top_n` users by that metric
         (default 5, no cap; the /ask Gemini exposure hardcodes
         top_n=5 by policy, but this DB function stays unconstrained
         for internal callers).

    Returns a dict shaped for tool-response consumption:
        {"users": [...], "count": int, ...optional metadata}
    Errors return {"error": "...", "users": []}.
    """
    conn = get_connection()

    # Helper: compute global racism-rank ordering. Mirrors
    # get_global_trader_ranks() shape. NULL or zero scores get no rank.
    def _global_racism_ranks() -> tuple[dict[int, int], int]:
        rows = conn.execute(
            """SELECT user_id FROM user_profiles
                WHERE racial_humor_score IS NOT NULL
                  AND racial_humor_score > 0
                ORDER BY racial_humor_score DESC, user_id ASC"""
        ).fetchall()
        return ({int(r["user_id"]): i + 1 for i, r in enumerate(rows)},
                len(rows))

    if username and username.strip():
        row = conn.execute(
            """SELECT user_id, display_name, username,
                      trader_rationale, racism_rationale
                 FROM user_profiles
                WHERE LOWER(username) = LOWER(?)""",
            (username.strip(),),
        ).fetchone()
        if not row:
            return {
                "error": f"No profile found for username '{username}'.",
                "users": [],
            }
        trader_ranks, trader_total = get_global_trader_ranks()
        racism_ranks, racism_total = _global_racism_ranks()
        uid = int(row["user_id"])
        return {
            "users": [{
                "username": row["username"],
                "display_name": row["display_name"],
                "user_id": uid,
                "trader_rank": trader_ranks.get(uid),
                "trader_rank_total": trader_total,
                "trader_rationale": row["trader_rationale"],
                "racism_rank": racism_ranks.get(uid),
                "racism_rank_total": racism_total,
                "racism_rationale": row["racism_rationale"],
            }],
            "count": 1,
            "mode": "single_user",
        }

    # Metric-based modes (rank_position OR top-N)
    metric = (metric or "").strip().lower()
    if metric not in ("trader", "racism"):
        return {
            "error": "Must specify either `username` for a single-user "
                     "lookup, or `metric` ('trader' or 'racism') for "
                     "a position / top-N lookup.",
            "users": [],
        }

    # Single-position mode: "who's #N" — no upper cap on N.
    # Returns the ONE user at that position with their rationale.
    # When from_bottom=True, OFFSET counts from the worst end (so
    # rank_position=1 returns the WORST-ranked user); the returned
    # `rank` field still reflects the user's true top-down position
    # so /ask can say "rank 49/49" cleanly.
    if rank_position is not None:
        try:
            pos = max(1, int(rank_position))
        except (TypeError, ValueError):
            return {
                "error": "rank_position must be a positive integer.",
                "users": [],
            }
        # Get total ranked count first (drives both bottom-up lookup
        # and the displayed rank when from_bottom=True).
        if metric == "trader":
            total = conn.execute(
                "SELECT COUNT(*) FROM user_profiles "
                "WHERE trader_score IS NOT NULL"
            ).fetchone()[0]
        else:
            total = conn.execute(
                "SELECT COUNT(*) FROM user_profiles "
                "WHERE racial_humor_score IS NOT NULL "
                "  AND racial_humor_score > 0"
            ).fetchone()[0]

        if pos > total:
            return {
                "error": f"No user at {metric}-rank #{pos} (fewer "
                         f"than {pos} users have a score).",
                "users": [],
            }

        if from_bottom:
            # Worst N from the bottom = OFFSET (total - pos) from the top.
            offset = max(0, total - pos)
            true_rank = total - pos + 1
        else:
            offset = pos - 1
            true_rank = pos

        if metric == "trader":
            r = conn.execute(
                """SELECT user_id, display_name, username,
                          trader_rationale
                     FROM user_profiles
                    WHERE trader_score IS NOT NULL
                    ORDER BY trader_score DESC, user_id ASC
                    LIMIT 1 OFFSET ?""",
                (offset,),
            ).fetchone()
        else:  # racism
            r = conn.execute(
                """SELECT user_id, display_name, username,
                          racism_rationale
                     FROM user_profiles
                    WHERE racial_humor_score IS NOT NULL
                      AND racial_humor_score > 0
                    ORDER BY racial_humor_score DESC, user_id ASC
                    LIMIT 1 OFFSET ?""",
                (offset,),
            ).fetchone()
        if not r:
            return {
                "error": f"No user at {metric}-rank #{pos} (fewer "
                         f"than {pos} users have a score).",
                "users": [],
            }
        user_payload = {
            "rank": true_rank,
            "rank_total": total,
            "metric": metric,
            "username": r["username"],
            "display_name": r["display_name"],
            "from_bottom": bool(from_bottom),
        }
        if metric == "trader":
            user_payload["trader_rationale"] = r["trader_rationale"]
        else:
            user_payload["racism_rationale"] = r["racism_rationale"]
        return {
            "users": [user_payload],
            "count": 1,
            "mode": "rank_position",
        }

    # Top-N leaderboard mode. No upper cap on top_n internally;
    # the /ask exposure hardcodes top_n=5 for policy.
    try:
        capped_n = max(1, int(top_n))
    except (TypeError, ValueError):
        capped_n = 5
    if metric == "trader":
        rows = conn.execute(
            """SELECT user_id, display_name, username, trader_rationale
                 FROM user_profiles
                WHERE trader_score IS NOT NULL
                ORDER BY trader_score DESC, user_id ASC
                LIMIT ?""",
            (capped_n,),
        ).fetchall()
        users = [
            {
                "rank": i + 1,
                "username": r["username"],
                "display_name": r["display_name"],
                "trader_rationale": r["trader_rationale"],
            }
            for i, r in enumerate(rows)
        ]
    else:  # racism
        rows = conn.execute(
            """SELECT user_id, display_name, username, racism_rationale
                 FROM user_profiles
                WHERE racial_humor_score IS NOT NULL
                  AND racial_humor_score > 0
                ORDER BY racial_humor_score DESC, user_id ASC
                LIMIT ?""",
            (capped_n,),
        ).fetchall()
        users = [
            {
                "rank": i + 1,
                "username": r["username"],
                "display_name": r["display_name"],
                "racism_rationale": r["racism_rationale"],
            }
            for i, r in enumerate(rows)
        ]
    return {
        "users": users,
        "count": len(users),
        "metric": metric,
        "mode": "top_n",
    }


def resolve_username_to_user_id(username: str | None) -> int | None:
    """Resolve a Discord username to a user_id, trying two sources in order.

    1. user_profiles.username — LLM-canonical, lowercase. Exact match,
       case-insensitive.
    2. chat_messages.author_username — most recent message row's
       author_id. Case-insensitive. Used when a member has chat
       activity but no profile yet.

    Returns None when neither source has an exact match, when input is
    empty / None / whitespace, or when DB access fails.
    """
    if not username:
        return None
    name = username.strip().lstrip("@")
    if not name:
        return None
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT user_id FROM user_profiles "
            "WHERE LOWER(username) = LOWER(?) LIMIT 1",
            (name,),
        ).fetchone()
        if row:
            return int(row["user_id"])
        row = conn.execute(
            "SELECT author_id FROM chat_messages "
            "WHERE LOWER(author_username) = LOWER(?) "
            "ORDER BY posted_at DESC LIMIT 1",
            (name,),
        ).fetchone()
        if row:
            return int(row["author_id"])
    except Exception as e:
        log.warning(f"resolve_username_to_user_id failed for {name!r}: {e}")
    return None


def get_user_profile_recent_trades_section(user_id: int) -> str:
    """Extract the "Recent trades" markdown section from a user's profile.

    The profile builder lays out user profiles with bold section headers:
      **Personality and style.**
      **Voice.**
      **Retarded takes.**
      **Recent trades.**
      **Recent personal life.**

    This helper pulls the body of the Recent trades section — the bullet
    lines beneath the heading — and returns them as a single string. The
    section ends at the next bold heading of the same shape (`**Word.**`)
    OR end-of-string.

    Returns "" when:
      - the user has no profile row
      - the profile_text is empty
      - the Recent trades heading is absent
      - DB access fails

    Tied to the profile prompt template's heading format. If that template
    changes (e.g. switches to `## Recent Trades` markdown headers), update
    the regex here too.
    """
    if not user_id:
        return ""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT profile_text FROM user_profiles WHERE user_id = ? LIMIT 1",
            (int(user_id),),
        ).fetchone()
    except Exception as e:
        log.warning(
            f"get_user_profile_recent_trades_section query failed for "
            f"user_id={user_id}: {e}"
        )
        return ""
    if not row:
        return ""
    text = row["profile_text"] or ""
    if not text:
        return ""
    import re as _re
    m = _re.search(
        r"\*\*Recent\s+trades\.\*\*\s*\n(.*?)(?=\n\*\*[A-Z][^*]*?\.\*\*|\Z)",
        text,
        flags=_re.IGNORECASE | _re.DOTALL,
    )
    if not m:
        return ""
    return m.group(1).strip()


def search_chat_messages_for_ask(
    keyword: str | None = None,
    *,
    days: int = 30,
    username: str | None = None,
    channel_name: str | None = None,
    start_iso: str | None = None,
    end_iso: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Substring search across chat_messages for /ask tool-calling.

    Three lookup modes, depending on which params are set:

      1. KEYWORD search (keyword set):
         Case-insensitive LIKE on content AND image_ocr_text within
         the trailing `days`-day window. Original behavior.
      2. TIME-WINDOW retrieval (start_iso AND end_iso set,
         keyword empty): returns ALL messages posted between
         start_iso and end_iso (UTC ISO strings), filtered by
         optional username/channel. No keyword needed — SV's
         "what was discussed between 5-9pm EST" lands here.
      3. KEYWORD + WINDOW (both keyword and start/end set):
         keyword match within the explicit window. Use when the
         asker references a topic at a specific time.

    Returns newest-first up to `limit` rows. Caller is responsible
    for the limit — recommended ~200 for time-window queries (more
    coverage), ~20-50 for keyword queries (matches are sparse).

    Used by Gemini's function-calling tool when the asker references
    historical chat content not present in the pre-injected
    subject-verbatim block. Lets the model "look up" what the room
    said about a topic / user / event / time-window without us
    trying to predict every possible lookup at prompt-build time.
    """
    from datetime import datetime, timedelta, timezone
    keyword = (keyword or "").strip() or None
    has_window = bool(start_iso) and bool(end_iso)
    if not keyword and not has_window:
        return []

    sql_parts: list[str] = [
        """SELECT discord_message_id, author_username, author_display,
                  channel_name, content, posted_at, image_ocr_text
             FROM chat_messages"""
    ]
    where: list[str] = []
    params: list = []

    if has_window:
        where.append("posted_at >= ?")
        params.append(start_iso)
        where.append("posted_at <= ?")
        params.append(end_iso)
    else:
        # Trailing days window (legacy keyword-only behavior).
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=int(days))
        ).isoformat()
        where.append("posted_at >= ?")
        params.append(cutoff)

    if keyword:
        needle = f"%{keyword.lower()}%"
        where.append(
            "(LOWER(COALESCE(content, '')) LIKE ? "
            "OR LOWER(COALESCE(image_ocr_text, '')) LIKE ?)"
        )
        params.extend([needle, needle])

    if username and username.strip():
        where.append("LOWER(author_username) = ?")
        params.append(username.strip().lower())
    if channel_name and channel_name.strip():
        where.append("channel_name = ?")
        params.append(channel_name.strip())

    sql_parts.append("WHERE " + " AND ".join(where))
    sql_parts.append("ORDER BY posted_at DESC LIMIT ?")
    params.append(int(limit))

    sql = " ".join(sql_parts)
    rows = get_connection().execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def find_user_messages_matching(
    username: str,
    needle: str,
    *,
    limit: int = 10,
) -> list[dict]:
    """Substring-match a user's actual messages (case-insensitive).
    Returns rows newest-first. Used by the /ask flow to produce
    verbatim receipts when a user challenges a claim with "show me
    where I said that."

    `needle` is matched as a LIKE pattern (callers can include
    SQLite wildcards if they want); helper auto-wraps with % when
    no wildcard char present.
    """
    if not username or not needle:
        return []
    needle = needle.strip()
    pattern = needle if any(c in needle for c in ("%", "_")) else f"%{needle}%"
    rows = get_connection().execute(
        """SELECT discord_message_id, channel_name, content, posted_at,
                  author_display, author_username
           FROM chat_messages
           WHERE LOWER(author_username) = LOWER(?)
             AND content LIKE ? COLLATE NOCASE
           ORDER BY posted_at DESC
           LIMIT ?""",
        (username.strip(), pattern, int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]


def get_recent_user_messages(
    username: str,
    *,
    limit: int = 50,
    channel_name: str | None = None,
) -> list[dict]:
    """Return a user's most recent messages, newest-first. Optionally
    scoped to a channel. Used by the profile-refresh pipeline to read
    from the local store instead of re-scanning Discord history.
    """
    if not username:
        return []
    if channel_name:
        rows = get_connection().execute(
            """SELECT discord_message_id, channel_id, channel_name, content,
                      posted_at, has_attachments, attachment_urls, embed_texts,
                      image_ocr_text, image_ocr_status
               FROM chat_messages
               WHERE LOWER(author_username) = LOWER(?)
                 AND channel_name = ?
               ORDER BY posted_at DESC
               LIMIT ?""",
            (username.strip(), channel_name, int(limit)),
        ).fetchall()
    else:
        rows = get_connection().execute(
            """SELECT discord_message_id, channel_id, channel_name, content,
                      posted_at, has_attachments, attachment_urls, embed_texts,
                      image_ocr_text, image_ocr_status
               FROM chat_messages
               WHERE LOWER(author_username) = LOWER(?)
               ORDER BY posted_at DESC
               LIMIT ?""",
            (username.strip(), int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


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
    row = get_connection().execute(
        f"""SELECT MAX(posted_at) AS latest
           FROM analyst_trades
           WHERE {' AND '.join(where)}""",
        tuple(params),
    ).fetchone()
    if row is None:
        return None
    val = row[0] if isinstance(row, tuple) else row["latest"]
    return val or None


def is_official_caller(author_id: int) -> bool:
    """Return True if this user_id maps to a configured analyst caller.

    Used by the points ledger to apply the caller-only nerf: official
    callers earn points ONLY on wins (entry+winning-close → +5, winning
    standalone screenshot → +2). Losses, ghosts, losing screenshots,
    and pending positions all earn 0 for callers. Members keep the
    full point table.

    The justification: callers are the product (members pay to tail
    them); their score should reward DOCUMENTED WINS, not chat presence
    or ghost-farmed open positions. BK had been accumulating ~30 ghost
    points per refresh from unposted closes; the nerf removes that
    while leaving real win documentation intact.

    Caller status is config-driven (config.py analyst_callers section,
    matched by username) — never derived from row counts, so a caller
    who took the week off doesn't lose their caller designation.
    Returns False on any lookup failure (treat as a member).
    """
    if not author_id:
        return False
    try:
        from config import settings
    except Exception:
        return False
    try:
        row = get_connection().execute(
            "SELECT username FROM user_profiles WHERE user_id = ?",
            (int(author_id),),
        ).fetchone()
    except Exception:
        return False
    if not row:
        return False
    uname = (row["username"] or "").strip().lower()
    if not uname:
        return False
    try:
        for c in settings.resolve_analyst_callers():
            if (c.get("username") or "").strip().lower() == uname:
                return True
    except Exception:
        pass
    return False


# Ledger constants (2026-07-01): scoring window widened to 21d with a
# recency band — wins ≤7d old score 2 pts, 8..21d score 1 pt. Ghosting
# stays a fixed 14d judgment about the position, independent of window.
_RECENT_WIN_DAYS = 7
_GHOST_AGE_DAYS = 14


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
    ghost_age_cutoff = (now - timedelta(days=_GHOST_AGE_DAYS)).isoformat()
    # Recency band: wins documented within the last 7d score 2 pts,
    # older wins (8..N d) score 1 pt.
    recent_win_cutoff = (now - timedelta(days=_RECENT_WIN_DAYS)).isoformat()
    # Join chat_messages to recover the source channel for each row.
    # The channel name is the disambiguator for close-only screenshots
    # where the order ticket doesn't carry a gain pill: gain-loss-porn
    # is structurally a wins channel (members post P&L flexes there),
    # so a close-only with gain_pct=None in that channel is presumed
    # a win, not a loss. Without the join we have to assume loss for
    # every gain-less close — which mis-scored DeeP FRieD as 11 losses
    # on what were actually his win-of-the-month series posts.
    rows = get_connection().execute(
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
                        reason.append(f"open ≥{_GHOST_AGE_DAYS}d")
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
                            f"before expiry, open <{_GHOST_AGE_DAYS}d)"
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
    caller_mode = is_official_caller(int(author_id))
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


def receipts_ceiling_from_points(points: int) -> int:
    """DEPRECATED — kept for backwards-compat with any external caller.

    The scoring system switched from min(base, ceiling) to additive
    (base + receipts, no receipts cap). This helper is no longer
    consumed by the profile builder. The pre-additive ceiling tier
    table is preserved below as historical reference only.

    Original docstring (kept for context — values are stale):
    Mapped rolling points → trader_score ceiling.

        0      points → 65   ("no receipts — can't certify edge")
        1-4    points → 70   ("starting to post; sliver above no-receipts")
        5-9    points → 75   ("real but sparse receipts; wins-only window")
        10-19  points → 85   ("documented edge; ceiling lifts substantially")
        20-29  points → 92   ("sustained two-sided posting; near-top")
        30+    points → 100  ("full receipt cadence; no ceiling")
    """
    p = max(0, int(points))
    if p == 0:
        return 65
    if p <= 4:
        return 70
    if p <= 9:
        return 75
    if p <= 19:
        return 85
    if p <= 29:
        return 92
    return 100


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
    rows = get_connection().execute(
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
    conn = get_connection()
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
