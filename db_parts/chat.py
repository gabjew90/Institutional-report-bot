"""Chat ingestion and members: chat_messages, user_profiles, protected users, catch-up watermarks, Sleeper players.

Moved verbatim from db.py on 2026-09-01. Every reference to a db.py
function goes through `_db.<name>` so the facade stays the single
patch point and the thread-local connection model lives in db.py.
"""
from datetime import datetime, date, timedelta
import logging

import db as _db  # noqa: E402

log = logging.getLogger("db")


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
        rows = _db.get_connection().execute(
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
        if not content or not _db._CHAT_TRADE_RE.search(content):
            continue
        out.append({
            "posted_at": (r["posted_at"] or "")[:16].replace("T", " "),
            "channel": r["channel_name"],
            "text": content[:180],
        })
        if len(out) >= limit:
            break
    return out


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
    last_full_rebuild_at: str | None = None,
) -> None:
    """Insert or replace a user profile. updated_at is auto-stamped.
    last_full_rebuild_at is stamped only on cold-start builds (None
    preserves the prior value via COALESCE).

    Metrics (slur_count, racial_humor_score, trader_score, trader_rationale,
    slur_examples, trader_examples) are part of the profile row.
    trader_rank is NOT set here — it's computed on-read via
    get_global_trader_ranks() (the stored column is deprecated).

    slur_examples and trader_examples are JSON-encoded list[str] payloads
    (use json.dumps in the caller). Stored as TEXT to keep schema simple.
    """
    conn = _db.get_connection()
    conn.execute(
        """INSERT INTO user_profiles
             (user_id, username, display_name, profile_text,
              message_count_at_update, last_seen_message_at,
              slur_count, racial_humor_score,
              trader_score, trader_rationale, racism_rationale,
              slur_examples, trader_examples, personal_ammo,
              last_full_rebuild_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
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
             last_full_rebuild_at = COALESCE(excluded.last_full_rebuild_at, user_profiles.last_full_rebuild_at),
             updated_at = datetime('now')""",
        (
            int(user_id), username, display_name, profile_text,
            int(message_count_at_update), last_seen_message_at,
            int(slur_count), racial_humor_score,
            trader_score, trader_rationale, racism_rationale,
            slur_examples, trader_examples, personal_ammo,
            last_full_rebuild_at,
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
    rows = _db.get_connection().execute(
        """SELECT user_id
           FROM user_profiles
           WHERE trader_score IS NOT NULL
           ORDER BY trader_score DESC, user_id ASC"""
    ).fetchall()
    rank_by_uid: dict[int, int] = {
        int(r["user_id"]): i + 1 for i, r in enumerate(rows)
    }
    return rank_by_uid, len(rows)


def get_user_profile(user_id: int) -> dict | None:
    row = _db.get_connection().execute(
        "SELECT * FROM user_profiles WHERE user_id = ?", (int(user_id),)
    ).fetchone()
    return dict(row) if row else None


def get_user_profile_by_username(username: str) -> dict | None:
    """Look up by case-insensitive username (Discord global username)."""
    row = _db.get_connection().execute(
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
    rows = _db.get_connection().execute(
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
    conn = _db.get_connection()
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
    rows = _db.get_connection().execute(
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
    trader_rank_by_uid, trader_rank_total = _db.get_global_trader_ranks()

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines: list[str] = [
        "# User profiles snapshot",
        "",
        "_Auto-generated by the daily user-profile refresh job._",
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
    rows = _db.get_connection().execute(
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


def get_promoted_protected_ids() -> set[int]:
    """Author IDs promoted from pending usernames (see protected_users
    table). Merged with settings.protected_user_id_set at ask time."""
    conn = _db.get_connection()
    return {r[0] for r in conn.execute(
        "SELECT author_id FROM protected_users")}


def maybe_promote_protected(
    username: str | None, author_id: int, pending: set[str],
) -> bool:
    """Pin a pending protected USERNAME to its permanent author_id on
    first sighting in ingested chat. Case-insensitive exact match; one
    promotion per username (INSERT OR IGNORE + UNIQUE(username)), so a
    released-and-reclaimed handle can never inherit protection. Returns
    True when a promotion happened."""
    u = (username or "").strip().lower()
    if not u or u not in pending:
        return False
    conn = _db.get_connection()
    cur = conn.execute(
        "INSERT OR IGNORE INTO protected_users (author_id, username) "
        "VALUES (?, ?)",
        (int(author_id), u),
    )
    conn.commit()
    if cur.rowcount > 0:
        log.info(
            f"protected: username {u!r} promoted to author_id "
            f"{author_id} on first sighting"
        )
        return True
    return False


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
    # Pending-protected promotion: this is the single funnel every
    # ingested message passes through, so the first message from a
    # pending username pins its permanent author_id here. Set-membership
    # check first — zero cost when the pending list is empty.
    try:
        from config import settings as _settings
        _pending = _settings.protected_pending_username_set
        if _pending:
            _db.maybe_promote_protected(author_username, author_id, _pending)
    except Exception as e:
        log.warning(f"protected promotion check failed (non-fatal): {e}")
    conn = _db.get_connection()
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


def find_oldest_chat_gap(
    channel_id: int,
    *,
    days: int = 30,
    gap_minutes: int = 60,
    since_iso: str | None = None,
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
    # Sealed-range floor (2026-08-23): a range already fully rescanned
    # (chat_catchup_watermark) is not re-examined — that is what stops
    # the permanent monthly rescan. See the table comment in the schema.
    if since_iso and str(since_iso) > cutoff:
        cutoff = str(since_iso)
    row = _db.get_connection().execute(
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
        row = _db.get_connection().execute(
            f"""SELECT COUNT(*) FROM chat_messages
                WHERE channel_name IN ({placeholders})
                  AND posted_at >= ?""",
            (*channel_names, cutoff),
        ).fetchone()
    else:
        row = _db.get_connection().execute(
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
        rows = _db.get_connection().execute(
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
        rows = _db.get_connection().execute(
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
    conn = _db.get_connection()
    cur = conn.execute(
        "DELETE FROM chat_messages WHERE posted_at < datetime('now', ?)",
        (f"-{int(days)} days",),
    )
    conn.commit()
    return cur.rowcount


def get_chat_message_row(discord_message_id: int) -> dict | None:
    """Fetch a single chat_messages row by Discord message ID. Used by
    the OCR helper to read the URL list + check the cached OCR text
    before deciding whether to call Gemini.
    """
    row = _db.get_connection().execute(
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
    conn = _db.get_connection()
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
        row = _db.get_connection().execute(
            "SELECT MAX(posted_at) FROM chat_messages WHERE channel_id = ?",
            (int(channel_id),),
        ).fetchone()
    else:
        row = _db.get_connection().execute(
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
    conn = _db.get_connection()

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
        trader_ranks, trader_total = _db.get_global_trader_ranks()
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
    conn = _db.get_connection()
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
    conn = _db.get_connection()
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
    # Shape C: username/channel_name alone is a valid call — recent
    # messages by that user / in that channel within the trailing
    # `days` window. This early return predated shape C (added at the
    # bot layer 2026-06-01) and silently killed it: the bot advertised
    # and validated the shape, then this line returned [] before the
    # filters below ever applied (found 2026-08-19 — the fantasy-league
    # IQ board fabricated 12 verdicts off an empty lookup while 384
    # matching rows sat in the table).
    has_entity_filter = bool((username or "").strip()) or bool(
        (channel_name or "").strip()
    )
    if not keyword and not has_window and not has_entity_filter:
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
    rows = _db.get_connection().execute(sql, params).fetchall()
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
    rows = _db.get_connection().execute(
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
        rows = _db.get_connection().execute(
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
        rows = _db.get_connection().execute(
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
        row = _db.get_connection().execute(
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


def upsert_sleeper_players(rows: list[tuple]) -> int:
    """Replace the sleeper_players cache with a fresh trimmed dump.
    Rows are (player_id, name, position, team). Full replace, not
    incremental — the dump is authoritative and ~11K rows is cheap.

    Anomaly guard: a suspiciously small dump (API hiccup returning a
    near-empty body) must NOT wipe a working cache — the real NFL dump
    is ~11K entries. Keep the stale cache and return 0 instead."""
    if len(rows) < _db.SLEEPER_DUMP_MIN_ROWS:
        return 0
    conn = _db.get_connection()
    conn.execute("DELETE FROM sleeper_players")
    conn.executemany(
        "INSERT OR REPLACE INTO sleeper_players "
        "(player_id, name, position, team, updated_at) "
        "VALUES (?, ?, ?, ?, datetime('now'))",
        rows,
    )
    conn.commit()
    return len(rows)


def get_sleeper_player_names(ids: list[str]) -> dict[str, str]:
    """player_id -> 'Name (POS, TEAM)' for the given ids. Missing ids
    are simply absent from the result — the caller shows the raw id."""
    ids = [str(i) for i in ids if i]
    if not ids:
        return {}
    out: dict[str, str] = {}
    # chunk to stay under SQLite's default 999-var limit
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        placeholders = ",".join("?" * len(chunk))
        rows = _db.get_connection().execute(
            f"SELECT player_id, name, position, team FROM sleeper_players "
            f"WHERE player_id IN ({placeholders})",
            chunk,
        ).fetchall()
        for r in rows:
            extra = ", ".join(x for x in (r["position"], r["team"]) if x)
            out[r["player_id"]] = (
                f"{r['name']} ({extra})" if extra else r["name"]
            )
    return out


def sleeper_players_cache_age_hours() -> float | None:
    """Hours since the cache was last refreshed; None when empty."""
    row = _db.get_connection().execute(
        "SELECT MAX(updated_at) AS ts, COUNT(*) AS n FROM sleeper_players"
    ).fetchone()
    if not row or not row["n"] or not row["ts"]:
        return None
    from datetime import datetime, timezone
    try:
        ts = datetime.fromisoformat(_db._normalize_ts(row["ts"]))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0
    except ValueError:
        return None


def get_catchup_watermark(channel_id: int) -> str | None:
    row = _db.get_connection().execute(
        "SELECT scanned_through FROM chat_catchup_watermark WHERE channel_id = ?",
        (int(channel_id),),
    ).fetchone()
    return row["scanned_through"] if row else None


def set_catchup_watermark(channel_id: int, scanned_through_iso: str) -> None:
    """Seal [.., scanned_through] for this channel: a later catchup only
    gap-scans past it. Only call after a FULL successful history walk."""
    conn = _db.get_connection()
    conn.execute(
        "INSERT INTO chat_catchup_watermark (channel_id, scanned_through, updated_at) "
        "VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(channel_id) DO UPDATE SET "
        "scanned_through = excluded.scanned_through, updated_at = excluded.updated_at",
        (int(channel_id), str(scanned_through_iso)),
    )
    conn.commit()
