"""The /ask tool layer: declarations (`_build_*_tool`) and executors
(`_execute_*`) for every function Gemini can call, plus the small helpers
only they use.

Extracted verbatim from discord_bot/bot.py on 2026-09-01 (review P2:
bot.py was 10.9k lines and this block was 2.1k of it). bot.py re-imports
every name here, so `bot._execute_x` still resolves for call sites, tests
and smokes. Routing text lives in discord_bot/tool_docs.py.
"""
from config import settings
from datetime import datetime, timedelta, timezone
from discord_bot.tool_docs import TOOL_DOCS as _TOOL_DOCS
import asyncio
import db
import discord
import logging
import pytz
import re
import time

log = logging.getLogger(__name__)


_CHAT_SEARCH_RESULT_LIMIT = 20


# Time-window queries return more rows because the asker wants
# coverage of an entire span, not just keyword matches. 200 caps
# the embed size at ~16k chars even on a busy channel hour.
_CHAT_TIME_WINDOW_RESULT_LIMIT = 200


def _build_chat_search_tool():
    """Construct the search_chat_messages FunctionDeclaration for the
    Gemini tools list. Lazy because google.genai.types import is heavy
    and we don't want module-load side effects."""
    from google.genai import types
    return types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="search_chat_messages",
                description=_TOOL_DOCS["search_chat_messages"] + (
                    "Search this Discord server's chat history. Two "
                    "shapes:\n"
                    "(A) KEYWORD search — pass `keyword` (and optionally "
                    "`days`, `username`, `channel_name`). Returns "
                    "matching messages within the trailing `days`-day "
                    "window. Use for 'what did kloh say about TSLA' or "
                    "'has BK ever mentioned QQQ'.\n"
                    "(B) TIME-WINDOW retrieval — pass `start_iso` AND "
                    "`end_iso` (and optionally `channel_name`), leave "
                    "`keyword` empty. Returns up to 200 messages "
                    "posted between those two UTC timestamps. Use for "
                    "'what was discussed between 5-9pm EST', 'summarize "
                    "the last hour of chat', 'recap this afternoon's "
                    "conversation'. You compute start_iso and end_iso "
                    "yourself by reading CURRENT TIME from the system "
                    "header (in UTC), converting any user-stated local "
                    "times accordingly, and formatting as ISO-8601 "
                    "(2026-05-31T22:00:00Z).\n"
                    "(C) USER / CHANNEL retrieval — pass `username` "
                    "OR `channel_name` (no `keyword`, no `start_iso`/"
                    "`end_iso`). Returns recent messages from that "
                    "user or in that channel within the trailing "
                    "`days` window (default 30, max 180). Use for "
                    "'what has Kyle been crying about today' / "
                    "'recap recent messages in #stonks-yapping' — "
                    "questions about a person or channel's recent "
                    "activity that don't have a specific keyword.\n"
                    "Use this ONLY when the asker references something "
                    "not already visible in your pre-injected context "
                    "(Recent channel chat covers only ~50 msgs / 24h of "
                    "THIS channel). Do NOT call for current events that "
                    "need Google Search."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "keyword": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "Shape A. Substring to match "
                                "(case-insensitive) against message "
                                "content AND OCR'd image text. Be "
                                "SPECIFIC — 'CRWV' or 'powell speech', "
                                "not generic words like 'the' or 'stock'. "
                                "Leave empty for shape B."
                            ),
                        ),
                        "days": types.Schema(
                            type=types.Type.INTEGER,
                            description=(
                                "Shape A. How many days back to search "
                                "for the keyword. Default 30. Hard cap "
                                "180 (chat retention window). Ignored "
                                "when start_iso/end_iso are set."
                            ),
                        ),
                        "username": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "Optional. Scope to this user's "
                                "messages only (use their Discord "
                                "username, not display name)."
                            ),
                        ),
                        "channel_name": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "Optional. Scope to a specific channel "
                                "name (e.g. '💬-stonks-yapping-💬'). "
                                "If unset on a time-window query, "
                                "returns chat across ALL ingested "
                                "channels."
                            ),
                        ),
                        "start_iso": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "Shape B. UTC ISO-8601 start of the "
                                "time window (e.g. "
                                "'2026-05-31T22:00:00Z' for 22:00 UTC "
                                "= 5pm EST). When set together with "
                                "end_iso, returns all messages in the "
                                "window (no keyword filter unless one "
                                "is also passed). YOU compute the UTC "
                                "value from CURRENT TIME in the system "
                                "header + the user's stated local time."
                            ),
                        ),
                        "end_iso": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "Shape B. UTC ISO-8601 end of the time "
                                "window. Must be later than start_iso. "
                                "See start_iso for the conversion rule."
                            ),
                        ),
                    },
                ),
            ),
        ],
    )


async def _execute_chat_search(args: dict) -> dict:
    """Run the search_chat_messages tool call against the local DB.
    Returns a dict shaped for Gemini's function_response part.

    Two shapes accepted (see tool description in _build_chat_search_tool):
      (A) keyword search — keyword required, trailing days window
      (B) time-window retrieval — start_iso AND end_iso required,
          keyword optional. Returns more rows (200 vs 20) since the
          asker wants window coverage rather than match density.
    """
    keyword = (args.get("keyword") or "").strip()
    start_iso = (args.get("start_iso") or "").strip() or None
    end_iso = (args.get("end_iso") or "").strip() or None
    has_window = bool(start_iso) and bool(end_iso)
    # Extract username + channel_name early so the shape-C validation
    # below can check them (was extracted later; moved up here).
    username = (args.get("username") or "").strip() or None
    channel_name = (args.get("channel_name") or "").strip() or None

    # Accept three shapes:
    #   A: keyword (optionally + username/channel/days) — keyword search
    #   B: start_iso + end_iso (optionally + keyword/channel) — time window
    #   C: username OR channel_name (no keyword, no window) — recent
    #      messages filtered by user or channel. Closes the "what has
    #      Kyle been crying about today" gap that needed keyword-invention
    #      before.
    if not keyword and not has_window and not username and not channel_name:
        return {
            "status": "error",
            "error": (
                "Provide at least one of: `keyword` (shape A), BOTH "
                "`start_iso` AND `end_iso` (shape B), or `username`/"
                "`channel_name` (shape C — recent messages by user / "
                "in channel)."
            ),
            "matches": [],
        }

    days = args.get("days") or 30
    try:
        days = max(1, min(180, int(days)))
    except (TypeError, ValueError):
        days = 30

    # Validate ISO timestamps shape so the tool can return a clean
    # error instead of leaking the SQL/parse error to the model. Accepts
    # the common Z-suffix form ('2026-05-31T22:00:00Z') and the
    # numeric-offset form ('2026-05-31T22:00:00+00:00').
    if has_window:
        from datetime import datetime as _dt
        for label, val in (("start_iso", start_iso), ("end_iso", end_iso)):
            try:
                # SQLite text-comparison only needs a stable ISO prefix;
                # explicit parse here is for validation feedback to the
                # model.
                _dt.fromisoformat(val.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                return {
                    "error": (
                        f"{label}={val!r} is not a valid ISO-8601 "
                        f"timestamp. Use UTC like "
                        f"'2026-05-31T22:00:00Z' or "
                        f"'2026-05-31T22:00:00+00:00'."
                    ),
                    "matches": [],
                }
        # SQLite chat_messages.posted_at is stored as ISO text. SQLite
        # text comparison treats Z-suffix and +00:00 differently from
        # each other and from the space-separator form. Normalize the
        # window bounds to the same shape as stored rows.
        from db import _normalize_ts
        start_iso = _normalize_ts(start_iso)
        end_iso = _normalize_ts(end_iso)
        limit = _CHAT_TIME_WINDOW_RESULT_LIMIT
    else:
        limit = _CHAT_SEARCH_RESULT_LIMIT

    # Compute the actual window bounds we queried so the response can
    # report them. Shape B already has start_iso/end_iso; shapes A and C
    # use a trailing `days` window we synthesize here so the model can
    # phrase "in the last N days from X to Y".
    from datetime import datetime as _dt2, timedelta as _td2, timezone as _tz2
    as_of_dt = _dt2.now(_tz2.utc)
    if has_window:
        window_start = start_iso
        window_end = end_iso
    else:
        window_end = as_of_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        window_start = (as_of_dt - _td2(days=days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    try:
        rows = db.search_chat_messages_for_ask(
            keyword=keyword or None,
            days=days,
            username=username,
            channel_name=channel_name,
            start_iso=start_iso,
            end_iso=end_iso,
            limit=limit,
        )
    except Exception as e:
        log.warning(f"search_chat_messages tool exec failed: {e}")
        return {
            "status": "error",
            "error": str(e)[:200],
            "matches": [],
            "window_start": window_start,
            "window_end": window_end,
        }

    # Time-window queries return newest-first per SQL; flip to
    # chronological for the model (easier to summarize a window
    # in order).
    if has_window:
        rows = list(reversed(rows))

    matches = [
        {
            "author": (
                r.get("author_display") or r.get("author_username") or "?"
            ),
            "username": r.get("author_username") or "",
            "channel": r.get("channel_name") or "",
            "timestamp": (r.get("posted_at") or "")[:16],
            "content": ((r.get("content") or "") + (
                f" [IMAGE-OCR: {r['image_ocr_text'][:200]}]"
                if r.get("image_ocr_text") else ""
            ))[:400],
        }
        for r in rows
    ]
    # Total-size cap (2026-06-10): a 200-row time-window result at
    # ~500 chars/row serializes to ~100KB inside ONE tool response —
    # blowing the context window for subsequent rounds. Cap the
    # serialized total at ~12KB by dropping the OLDEST rows (rows are
    # newest-first); record how many were dropped so the model can say
    # "showing the most recent N of M".
    _CHAT_RESULT_MAX_CHARS = 12_000
    truncated_from = None
    serialized = sum(len(str(m)) for m in matches)
    if serialized > _CHAT_RESULT_MAX_CHARS and len(matches) > 1:
        truncated_from = len(matches)
        running = 0
        kept: list[dict] = []
        for m in matches:
            running += len(str(m))
            if running > _CHAT_RESULT_MAX_CHARS:
                break
            kept.append(m)
        matches = kept or matches[:1]
    if has_window:
        log.info(
            f"chat_search tool (window): {start_iso} → {end_iso} "
            f"channel={channel_name!r} username={username!r} "
            f"keyword={keyword!r} → {len(matches)} rows"
        )
    else:
        log.info(
            f"chat_search tool (keyword): keyword={keyword!r} days={days} "
            f"username={username!r} channel={channel_name!r} → "
            f"{len(matches)} matches"
        )
    result = {
        "status": "ok" if matches else "empty",
        "matches": matches,
        "count": len(matches),
        "window_start": window_start,
        "window_end": window_end,
        # Empty is a RESULT, not a shrug (2026-08-19: an empty lookup on
        # the fantasy channel was followed by 12 invented per-member
        # verdicts — the model treated no-rows the same as no-call and
        # fell back to profile priors). Tell it explicitly what an empty
        # result obligates.
        **({} if matches else {"note": (
            "No messages matched these filters. If your answer depends "
            "on this lookup, SAY the search came back empty — do NOT "
            "invent chat content, takes, or behavior you did not "
            "retrieve. Consider retrying with a different keyword or a "
            "wider window before giving up."
        )}),
        "filters": {
            "keyword": keyword or None,
            "days": days if not has_window else None,
            "username": username,
            "channel_name": channel_name,
            "start_iso": start_iso,
            "end_iso": end_iso,
        },
    }
    if truncated_from is not None:
        result["truncated"] = (
            f"showing the {len(matches)} most recent of {truncated_from} "
            f"matches (size cap) — narrow the window or add a keyword "
            f"for the rest"
        )
    return result


def _build_user_profile_tool():
    """FunctionDeclaration for `lookup_user_profile`. Unifies the three
    modes from the legacy `lookup_user_profile` tool and adds an
    `include_profile` flag that returns the full WHO'S TALKING dossier
    on top of rank + rationales.

    Anchors (exactly one required):
      - username: specific user
      - metric: "trader" | "racism" — leaderboard or rank_position lookup
      - metric + rank_position: the ONE user at that rank position

    include_profile=True: also include the user's full profile_text
    (Personality + Voice + Retarded Takes + Recent Personal Life +
    Recent Trades). Rejected in leaderboard mode (5 dossiers too big).
    """
    from google.genai import types
    return types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="lookup_user_profile",
                description=_TOOL_DOCS["lookup_user_profile"] + (
                    "Look up rank + optional full profile for a "
                    "Discord room member. Three anchor shapes "
                    "(use EXACTLY one):\n"
                    "(a) `username` set → returns that user's "
                    "trader-rank, racism-rank, and both rationales. "
                    "Use when asker names a specific user.\n"
                    "(b) `metric` ('trader' or 'racism') + "
                    "`rank_position` (positive integer, no upper "
                    "cap) → returns the ONE user at that rank. Use "
                    "for 'who's #N' questions.\n"
                    "(c) `metric` set with no rank_position → "
                    "returns the TOP 5 leaderboard. Use for "
                    "leaderboard-style asks ('top 5 traders', "
                    "'who's the most annoying').\n"
                    "Set `include_profile=true` on (a) or (b) to also "
                    "return the user's full personality dossier "
                    "(Personality + Voice + Retarded Takes + Recent "
                    "Personal Life). Use when the question needs "
                    "personality / voice / personal context. Rejected "
                    "in leaderboard mode (5 dossiers too big).\n"
                    "Add from_bottom=true with rank_position to count "
                    "from the worst end ('worst trader' → "
                    "rank_position=1, from_bottom=true).\n"
                    "Never quote raw 0-100 scores."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "username": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "Discord username (lowercase, no @). "
                                "Mode (a). Mutually exclusive with metric."
                            ),
                        ),
                        "metric": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "'trader' or 'racism'. Modes (b) and (c). "
                                "Mutually exclusive with username."
                            ),
                        ),
                        "rank_position": types.Schema(
                            type=types.Type.INTEGER,
                            description=(
                                "1-based rank position (any N — no cap). "
                                "Used with metric for mode (b)."
                            ),
                        ),
                        "include_profile": types.Schema(
                            type=types.Type.BOOLEAN,
                            description=(
                                "When true, also return the user's full "
                                "profile dossier. Rejected in mode (c)."
                            ),
                        ),
                        "from_bottom": types.Schema(
                            type=types.Type.BOOLEAN,
                            description=(
                                "Used with rank_position. When true, "
                                "rank_position counts from the worst "
                                "end. Ignored without rank_position."
                            ),
                        ),
                        "top_n": types.Schema(
                            type=types.Type.INTEGER,
                            description=(
                                "Leaderboard mode (c) only: number of "
                                "users to return when the asker names a "
                                "size ('top 10'). Default 5, max 10."
                            ),
                        ),
                    },
                ),
            ),
        ],
    )


async def _execute_user_profile(args: dict) -> dict:
    """Run the lookup_user_profile tool call.

    Validates anchor exclusivity, delegates rank lookup to
    db.lookup_user_ranks (same query the legacy tool used), then
    optionally enriches each returned user with their full profile
    dossier via db.format_user_profiles_for_context.
    """
    username = (args.get("username") or "").strip() or None
    metric_raw = args.get("metric")
    metric = (metric_raw or "").strip() or None if metric_raw is not None else None
    rank_position = args.get("rank_position")
    include_profile = bool(args.get("include_profile"))
    from_bottom = bool(args.get("from_bottom"))

    # Top-level freshness stamp — when this tool call ran. Per-user
    # `updated_at` (when their profile was last refreshed) is filled in
    # below, after we have user_ids.
    from datetime import datetime as _dt2, timezone as _tz2
    as_of = _dt2.now(_tz2.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Validation: exactly one anchor.
    if username and metric:
        return {
            "status": "error",
            "as_of": as_of,
            "error": (
                "Provide exactly one anchor: either `username` "
                "(specific user), or `metric` ('trader'/'racism') "
                "with optional `rank_position`."
            ),
            "users": [],
        }
    if not username and not metric:
        return {
            "status": "error",
            "as_of": as_of,
            "error": (
                "Must provide either `username` (single user), or "
                "`metric` ('trader' / 'racism'), optionally with "
                "`rank_position` for a specific #N lookup."
            ),
            "users": [],
        }

    # Leaderboard mode rejects include_profile (5 dossiers too big).
    if metric and rank_position is None and include_profile:
        return {
            "status": "error",
            "as_of": as_of,
            "error": (
                "include_profile is not supported in leaderboard mode "
                "(5 dossiers is too large). Ask for a specific user "
                "with `username=<...>, include_profile=true` instead, "
                "or for a single ranked user via `metric=<...>, "
                "rank_position=N, include_profile=true`."
            ),
            "users": [],
        }

    try:
        # Leaderboard size: honor the asked-for N, default 5, hard cap
        # 10 (2026-07-29 — kyle asked "top 10", got a silent 5; the
        # full-roster blast radius still argues for a ceiling).
        # rank_position mode has no cap on N.
        try:
            _top_n = int(args.get("top_n") or 5)
        except Exception:
            _top_n = 5
        _top_n = max(1, min(10, _top_n))
        result = db.lookup_user_ranks(
            username=username,
            metric=metric,
            rank_position=rank_position,
            top_n=_top_n,
            from_bottom=from_bottom,
        )
    except Exception as e:
        log.warning(f"lookup_user_profile rank lookup failed: {e}")
        return {
            "status": "error",
            "as_of": as_of,
            "error": f"rank lookup failed: {type(e).__name__}: {e}",
            "users": [],
        }

    if "error" in result:
        # Username miss / rank-position OOB / metric-invalid — query
        # ran cleanly, just no row matched. Tag as not_found so the
        # model says "no data" rather than fabricating, but
        # distinguishably from a true runtime error.
        result["status"] = "not_found"
        result["as_of"] = as_of
        return result

    # Per-user updated_at: when each profile row was last refreshed.
    # Lets the model say "as of 2 days ago" when a profile is stale
    # instead of treating everything as current.
    if result.get("users"):
        try:
            conn = db.get_connection()
            uids = [
                int(u["user_id"]) for u in result["users"]
                if u.get("user_id") is not None
            ]
            if uids:
                placeholders = ",".join("?" * len(uids))
                rows = conn.execute(
                    f"SELECT user_id, updated_at FROM user_profiles "
                    f"WHERE user_id IN ({placeholders})",
                    uids,
                ).fetchall()
                updated_map = {
                    int(r["user_id"]): r["updated_at"] for r in rows
                }
                for u in result["users"]:
                    uid = u.get("user_id")
                    if uid is not None:
                        u["updated_at"] = updated_map.get(int(uid))
        except Exception as e:
            log.warning(f"lookup_user_profile updated_at enrich failed: {e}")

    # If include_profile=True, enrich each returned user with their
    # full dossier. format_user_profiles_for_context handles missing
    # profiles gracefully (returns "" for users without a profile row).
    if include_profile and result.get("users"):
        for user in result["users"]:
            uid = user.get("user_id")
            if not uid:
                continue
            try:
                dossier = db.format_user_profiles_for_context([int(uid)])
            except Exception as e:
                log.warning(
                    f"lookup_user_profile dossier fetch failed for user_id={uid}: {e}"
                )
                dossier = ""
            user["profile_text"] = (dossier or "").strip()

    result["status"] = "ok" if result.get("users") else "empty"
    result["as_of"] = as_of
    log.info(
        f"user_profile tool: username={username!r} metric={metric!r} "
        f"rank_position={rank_position!r} include_profile={include_profile} "
        f"→ mode={result.get('mode')} count={result.get('count')} "
        f"status={result.get('status')}"
    )
    return result


def _build_trade_log_tool():
    """FunctionDeclaration for `lookup_trade_log`. Works for two anchors:

    - `caller`: a registered analyst caller (e.g. 'abe', 'bankerkyle').
      Queries analyst_trades caller-mode rows. High fidelity — daily
      cron stitches open/close pairs. Returns ONLY the log data.

    - `username`: any Discord username. Resolves to user_id and pulls
      the 'Recent trades' section from that user's profile. Member-mode
      data quality — no per-trade stitching.

    Exactly one anchor must be provided. `kind` ∈ {open, recent, tally, all}
    slices the response on the caller path. `days` overrides defaults
    (7 for kind=recent, 30 for kind=tally; ignored for kind=open).
    """
    from google.genai import types
    return types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="lookup_trade_log",
                description=_TOOL_DOCS["lookup_trade_log"] + (
                    "Look up trade history. Two anchors (use EXACTLY one):\n"
                    "(a) `caller`: registered analyst caller name "
                    "('abe', 'bankerkyle', ...). Returns structured "
                    "trade log with daily-cron-stitched W/L. Use for "
                    "Abe / BK specifically — their data is high "
                    "fidelity.\n"
                    "(b) `username`: any other Discord username. "
                    "Returns the user's 'Recent trades' snippet from "
                    "their profile. Member-mode fidelity (no W/L "
                    "stitching). Use for non-caller users.\n"
                    "`kind` ∈ {'open','recent','tally','all'} (default "
                    "'all'). 'open' = current open positions only. "
                    "'recent' = last N days of trade events. 'tally' = "
                    "W/L summary. 'all' = everything.\n"
                    "`days` overrides defaults (7 for kind=recent, 30 "
                    "for kind=tally; ignored for kind=open / kind=all)."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "caller": types.Schema(
                            type=types.Type.STRING,
                            description="Registered caller: 'abe', 'bankerkyle', ...",
                        ),
                        "username": types.Schema(
                            type=types.Type.STRING,
                            description="Any other Discord username.",
                        ),
                        "kind": types.Schema(
                            type=types.Type.STRING,
                            description="open | recent | tally | all (default all)",
                        ),
                        "days": types.Schema(
                            type=types.Type.INTEGER,
                            description="Window override in days (1..180).",
                        ),
                    },
                ),
            )
        ]
    )


async def _execute_trade_log(args: dict) -> dict:
    """Run the lookup_trade_log tool call.

    Caller path: queries db.format_analyst_trades_for_context with the
    requested kind, returns the rendered block.

    Username path: resolves username → user_id, pulls the Recent trades
    snippet from the user's profile. (Member-mode rows in analyst_trades
    are not joined in v1; defer until QC shows it's worth the SQL
    layer change.)
    """
    caller = (args.get("caller") or "").strip() or None
    username = (args.get("username") or "").strip() or None
    kind = (args.get("kind") or "all").strip().lower()
    days_arg = args.get("days")

    # Freshness stamp on every response shape — top-level. Caller path
    # also gets window_days, username path also gets profile_updated_at
    # (since member fidelity = whatever was true at profile-refresh
    # time, not now).
    from datetime import datetime as _dt2, timezone as _tz2
    as_of = _dt2.now(_tz2.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Validation: anchor exclusivity.
    if caller and username:
        return {
            "status": "error",
            "as_of": as_of,
            "error": (
                "Provide exactly one of `caller` or `username` "
                "(got both)."
            ),
        }
    if not caller and not username:
        return {
            "status": "error",
            "as_of": as_of,
            "error": (
                "Provide exactly one of `caller` (registered "
                "analyst — 'abe', 'bankerkyle', ...) or `username` "
                "(any other Discord username)."
            ),
        }
    if kind not in ("all", "open", "recent", "tally"):
        return {
            "status": "error",
            "as_of": as_of,
            "error": f"`kind` must be one of: all, open, recent, tally; got {kind!r}.",
        }

    # Default windows per kind.
    if kind == "recent":
        days = int(days_arg) if days_arg else 7
    elif kind == "tally":
        days = int(days_arg) if days_arg else 30
    else:
        days = 7  # placeholder for kind=open/all (ignored by the formatter)
    days = max(1, min(180, days))

    # --- CALLER ANCHOR ---
    if caller:
        # Resolve display name from the configured caller registry.
        display = None
        try:
            for c in settings.resolve_analyst_callers():
                if c.get("name", "").lower() == caller.lower():
                    display = c.get("display") or caller.title()
                    break
        except Exception as e:
            log.warning(f"lookup_trade_log caller registry lookup failed: {e}")
        display = display or caller.title()
        try:
            text = db.format_analyst_trades_for_context(
                hours=days * 24,
                caller=caller.lower(),
                display=display,
                tracking_mode="caller",
                kind=kind,
            )
        except ValueError as e:
            # ValueError = malformed arg / unknown caller — clean
            # failure mode, not a runtime crash. Tag not_found so the
            # model says "couldn't find that caller" cleanly.
            return {
                "status": "not_found",
                "as_of": as_of,
                "window_days": days,
                "error": str(e),
            }
        except Exception as e:
            log.warning(f"lookup_trade_log caller path failed: {e}")
            return {
                "status": "error",
                "as_of": as_of,
                "window_days": days,
                "error": f"{type(e).__name__}: {e}",
            }
        if not text:
            return {
                "status": "empty",
                "as_of": as_of,
                "window_days": days,
                "anchor": {"type": "caller", "name": caller},
                "kind": kind,
                "data_quality": "caller",
            }
        return {
            "status": "ok",
            "as_of": as_of,
            "window_days": days,
            "anchor": {"type": "caller", "name": caller, "display": display},
            "kind": kind,
            "data_quality": "caller",
            "trades_text": text,
        }

    # --- USERNAME ANCHOR ---
    try:
        user_id = db.resolve_username_to_user_id(username)
    except Exception as e:
        log.warning(f"lookup_trade_log resolve_username_to_user_id failed: {e}")
        return {
            "status": "error",
            "as_of": as_of,
            "window_days": days,
            "error": f"{type(e).__name__}: {e}",
        }
    if user_id is None:
        return {
            "status": "not_found",
            "as_of": as_of,
            "window_days": days,
            "error": f"username {username!r} not found in profiles or chat history.",
        }

    # profile_updated_at: when this user's profile (the source of the
    # Recent trades section) was last refreshed. Stale-snapshot hint.
    profile_updated_at = None
    try:
        row = db.get_connection().execute(
            "SELECT updated_at FROM user_profiles WHERE user_id = ?",
            (int(user_id),),
        ).fetchone()
        if row:
            profile_updated_at = row["updated_at"]
    except Exception as e:
        log.warning(f"lookup_trade_log profile_updated_at fetch failed: {e}")

    try:
        profile_snippet = db.get_user_profile_recent_trades_section(user_id)
    except Exception as e:
        log.warning(f"lookup_trade_log profile snippet fetch failed: {e}")
        return {
            "status": "error",
            "as_of": as_of,
            "window_days": days,
            "profile_updated_at": profile_updated_at,
            "anchor": {"type": "username", "name": username, "user_id": user_id},
            "kind": kind,
            "data_quality": "member",
            "error": f"{type(e).__name__}: {e}",
        }
    # Chat-stated trades — the member's own recent messages that read as
    # trade calls. Most of the room trades by TALKING, not screenshotting,
    # so the ledger/profile snippet alone made the bot tell active
    # traders "you did nothing" (2026-06-17: terlin called META puts
    # +100% in chat, got "zero mentions of META"). These are
    # self-reported, NOT screenshot-verified — labeled as such.
    chat_stated_trades: list[dict] = []
    try:
        chat_stated_trades = db.get_recent_user_chat_trades(
            user_id, days=max(int(days), 2)
        )
    except Exception as e:
        log.warning(f"lookup_trade_log chat-stated fetch failed: {e}")

    if not profile_snippet and not chat_stated_trades:
        return {
            "status": "empty",
            "as_of": as_of,
            "window_days": days,
            "profile_updated_at": profile_updated_at,
            "anchor": {"type": "username", "name": username, "user_id": user_id},
            "kind": kind,
            "data_quality": "member",
        }
    return {
        "status": "ok",
        "as_of": as_of,
        "window_days": days,
        "profile_updated_at": profile_updated_at,
        "anchor": {"type": "username", "name": username, "user_id": user_id},
        "kind": kind,
        "data_quality": "member",
        "profile_recent_trades": profile_snippet or None,
        # Self-reported (NOT screenshot-verified). Their own chat words.
        "chat_stated_trades": chat_stated_trades,
    }


# Hardcoded crypto symbol allowlist. Extensible — append symbols here
# as crypto questions surface them. Anything not in this set routes
# to Finnhub (stocks/ETFs/indices).
# Crypto-PRIORITY set: symbols routed to Binance.US FIRST (before the
# stock path), because they're unambiguous majors we never want
# mis-resolved to a same-letter stock. This is NO LONGER the ceiling on
# crypto coverage — any symbol that isn't a valid US stock gets a
# Binance.US fallback (see _crypto_quote + the executor), so SUI/PEPE/
# TON/ARB/new-listings resolve dynamically. (2026-07-29: was a hard
# 10-coin allowlist; long-tail coins silently missed in a crypto room.)
_CRYPTO_SYMBOLS = frozenset({
    "BTC", "ETH", "SOL", "DOGE", "ADA", "AVAX", "XRP", "BNB", "LINK",
    "LTC", "DOT", "TRX", "SUI", "TON", "ARB", "OP", "APT", "NEAR",
})


async def _crypto_quote(sym: str) -> dict | None:
    """A live Binance.US quote for `sym` (as {SYM}USDT), or None if the
    pair doesn't exist / the fetch fails. The single crypto builder used
    by both the priority path and the stock-miss fallback."""
    from report import market_data as _md
    try:
        data = await asyncio.to_thread(_md._fetch_binance_24h, f"{sym}USDT")
    except Exception:
        return None
    if not data:
        return None
    return {
        "symbol": sym,
        "price": data.get("price"),
        "change_pct": data.get("change_24h_rolling"),
        "prev_close": None,
        "source": "binance",
        # Crypto trades 24/7 — Binance.US price is always live regardless
        # of US-market session; tag it so Gemini doesn't false-stale it
        # when mixed with after-hours stock quotes in one batch.
        "data_freshness": "live_24_7",
    }


def _build_economic_calendar_tool():
    """FunctionDeclaration for `lookup_economic_calendar`. Reads from
    Finnhub's `/calendar/economic` endpoint — the SAME source the daily
    pulse uses — so /ask answers stay consistent with the pulse's
    macro numbers. Closes the 2026-06-05 NFP cross-source conflict
    (pulse said 120k ADP, /ask said 172k via Google grounding) and
    the recurring macro-print fabrication family.
    """
    from google.genai import types
    return types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="lookup_economic_calendar",
                description=_TOOL_DOCS["lookup_economic_calendar"] + (
                    "Canonical scheduled-time + consensus + previous + "
                    "actual values for US Tier-1 macro releases (CPI, "
                    "PCE, NFP / payrolls, unemployment, GDP, retail "
                    "sales, ISM, PPI, FOMC, Powell) and major foreign "
                    "rate decisions (ECB, BOJ, BOE). Same Finnhub "
                    "source the daily pulse uses, so the numbers you "
                    "get here MATCH the pulse — no cross-source "
                    "drift.\n\n"
                    "USE for: 'when is CPI', 'what's NFP consensus', "
                    "'May payrolls actual', 'ECB next decision', "
                    "'last 3 CPI prints', 'what does street expect "
                    "for retail sales', 'what was last PCE', 'when is "
                    "next Powell speech'.\n\n"
                    "DO NOT use for: forecaster-specific reads ('what "
                    "does Goldman expect for CPI' — that needs Google "
                    "Search), market reaction commentary, or non-Tier-"
                    "1 prints (regional Fed surveys, minor housing "
                    "data, foreign macro without US linkage — those "
                    "are filtered out of this tool's whitelist and "
                    "won't return).\n\n"
                    "Args:\n"
                    "  query: optional case-insensitive event name "
                    "filter (e.g. 'CPI' / 'NFP' / 'ECB' / 'May "
                    "payrolls'). Omit to get all Tier-1 events in "
                    "the window.\n"
                    "  days_window: optional ±days from today (default "
                    "14 — covers 'this week' + 'last week's print'). "
                    "Range 1-30.\n\n"
                    "Response shape: {status, events: [...], as_of}.\n"
                    "Each event row has: event, country, "
                    "scheduled_iso_utc, scheduled_et_human, impact, "
                    "consensus, prev, actual, unit, status. "
                    "`status` = 'released' (actual present) / "
                    "'scheduled' (future, consensus may or may not be "
                    "posted) / 'past_no_data' (after schedule, no "
                    "actual yet — common in the 30-60 min between "
                    "scheduled time and the BLS/BEA release wire).\n\n"
                    "If `consensus` is null on a 'scheduled' event, "
                    "broker desks haven't published consensus yet — "
                    "tell the asker 'no consensus posted yet', do "
                    "NOT pull a forecast from Google."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "query": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "Optional event-name filter (e.g. "
                                "'CPI', 'NFP', 'ECB', 'May "
                                "payrolls'). Omit for full Tier-1 "
                                "window."
                            ),
                        ),
                        "days_window": types.Schema(
                            type=types.Type.INTEGER,
                            description=(
                                "Optional ±days from today (default "
                                "14, range 1-30)."
                            ),
                        ),
                    },
                ),
            )
        ]
    )


async def _execute_economic_calendar(args: dict) -> dict:
    """Run the lookup_economic_calendar tool call.

    Returns LLM-ready dict with status / events list / as_of timestamp.
    Empty events list returns status='no_match' so the model tells the
    asker no event found, rather than fabricating one from memory.
    """
    from datetime import datetime
    from report import news_data as _nd

    query = (args.get("query") or "").strip() or None
    try:
        days_window = int(args.get("days_window") or 14)
    except (TypeError, ValueError):
        days_window = 14
    days_window = max(1, min(30, days_window))

    try:
        # to_thread: the fetcher does synchronous urllib I/O. Calling it
        # directly on the event loop freezes ALL bot activity (message
        # ingestion, other /asks, OCR) for the request duration.
        events = await asyncio.to_thread(
            _nd.fetch_economic_calendar_structured,
            query=query, days_window=days_window,
        )
    except Exception as e:
        # Includes EconomicCalendarUnavailable (Finnhub + ForexFactory
        # both down). The distinction matters: this is "the FEED is
        # down", never "that event doesn't exist".
        log.warning(f"lookup_economic_calendar: fetcher raised: {e}")
        return {
            "status": "error",
            "error": (
                "Economic-calendar feeds are down (Finnhub blocked and "
                "fallback unreachable). No live calendar data — tell "
                "the asker the calendar feed isn't available right "
                "now, then FALL BACK TO GOOGLE SEARCH for the specific "
                "date/print they asked about and answer the actual "
                "question. Do NOT claim the event doesn't exist."
            ),
        }

    if not events:
        return {
            "status": "no_match",
            "query": query,
            "days_window": days_window,
            "error": (
                "No Tier-1 macro events found for that query / window. "
                "Tier-1 covers: CPI, PCE, NFP / payrolls, unemployment, "
                "GDP, retail sales, ISM, PPI, FOMC + Powell speeches, "
                "ECB/BOJ/BOE rate decisions. Anything outside that set "
                "(regional Fed surveys, minor housing data, foreign "
                "macro without US linkage) is filtered out. NOTE: if "
                "the feed is in fallback mode it only covers the "
                "current calendar week — for events outside that "
                "window, use Google Search and answer the actual "
                "question."
            ),
        }

    resp = {
        "status": "ok",
        "query": query,
        "days_window": days_window,
        "events": events[:30],
        "as_of": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    }
    sources = {e.get("source") for e in events[:30]}
    if sources & {"forexfactory", "fred"}:
        resp["coverage_note"] = (
            "FALLBACK MODE (Finnhub calendar down). Consensus estimates "
            "exist only for the CURRENT CALENDAR WEEK (ForexFactory "
            "rows); rows sourced 'fred' are official release dates "
            "beyond this week with NO consensus — say 'no consensus "
            "posted yet', do NOT invent one. Where `actual` is present "
            "it is an official FRED number and `actual_period` names "
            "the reference month — quote it as that month's print. For "
            "anything still missing, use Google Search and answer the "
            "asker's actual question."
        )
    return resp


async def _execute_price_history(args: dict) -> dict:
    """Run the lookup_price_history tool call — daily/weekly closes for
    one symbol. The ONLY historical market series available; without it
    the model fabricated weekly S&P closes for a correlation chart
    (2026-07-29)."""
    from report import market_data as _md

    symbol = str(args.get("symbol") or "").strip().upper()
    if not symbol:
        return {"status": "error", "error": "symbol is required"}
    start = str(args.get("start") or "").strip()
    end = str(args.get("end") or "").strip() or None
    interval = str(args.get("interval") or "1d").strip().lower()
    if interval not in ("1d", "1wk", "1mo"):
        interval = "1d"
    if not start:
        from datetime import timedelta
        start = (datetime.utcnow() - timedelta(days=90)).strftime("%Y-%m-%d")
    try:
        hist = await asyncio.to_thread(
            _md.fetch_price_history, symbol, start, end, interval
        )
    except Exception as e:
        return {"status": "error",
                "error": f"{type(e).__name__}: {str(e)[:160]}"}
    if not hist:
        return {
            "status": "no_data",
            "symbol": symbol,
            "error": (
                f"no price history for {symbol} over that window — say so, "
                f"do NOT invent a series"
            ),
        }
    return {
        "status": "ok",
        "symbol": symbol,
        "interval": interval,
        "points": hist[-400:],
        "count": len(hist[-400:]),
        "as_of": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    }


def _build_price_history_tool():
    """FunctionDeclaration for `lookup_price_history` — historical closes
    for ONE symbol (stocks/ETFs/indices via Yahoo)."""
    from google.genai import types
    return types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="lookup_price_history",
                description=_TOOL_DOCS["lookup_price_history"] + (
                    "Historical daily/weekly CLOSES for one stock, ETF, "
                    "or index — the only source of market price HISTORY "
                    "you have (lookup_market_price is current-only). Use "
                    "it whenever an analysis needs a time series: "
                    "performance since a date, a drawdown, a chart over "
                    "time, or ANY correlation against another series. "
                    "Returns full OHLC — [{date, open, high, low, close, "
                    "volume}] oldest-first — so you can draw real "
                    "candlesticks, not just a close line.\n\n"
                    "Index tickers use the Yahoo caret form: ^GSPC "
                    "(S&P 500), ^NDX (Nasdaq 100), ^DJI, ^RUT, ^VIX. "
                    "Plain tickers for everything else (SPY, NVDA, BNO).\n\n"
                    "Args: symbol; start (ISO 'YYYY-MM-DD', default 90d "
                    "ago); end (ISO, optional = today); interval "
                    "('1d'|'1wk'|'1mo', default '1d').\n\n"
                    "`status: 'no_data'` means no series exists for that "
                    "symbol/window — SAY SO; never invent price levels "
                    "to fill a chart axis."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "symbol": types.Schema(
                            type=types.Type.STRING,
                            description="Ticker, e.g. 'SPY' or '^GSPC'.",
                        ),
                        "start": types.Schema(
                            type=types.Type.STRING,
                            description="ISO start date 'YYYY-MM-DD'.",
                        ),
                        "end": types.Schema(
                            type=types.Type.STRING,
                            description="ISO end date (optional).",
                        ),
                        "interval": types.Schema(
                            type=types.Type.STRING,
                            description="'1d' | '1wk' | '1mo'.",
                        ),
                    },
                    required=["symbol"],
                ),
            )
        ]
    )


# Inline mime types Gemini accepts on a request. Code execution can
# emit OTHER artifacts (a saved .npy/.csv comes back as
# application/octet-stream); echoing one of those back into `contents`
# on the next tool round 400s the whole request with "Unsupported MIME
# type: application/octet-stream" — which the user sees as "something
# broke the model" (2026-07-29, "analyze trades ... relative to qqq").
_ECHO_SAFE_INLINE_PREFIXES = ("image/", "application/pdf")


def _safe_echo_parts(parts):
    """Drop response parts that can't be sent back to the API.

    Keeps text / executable_code / code_execution_result / function_call
    (the tool loop needs them) and any inline_data the API accepts;
    drops unsupported inline artifacts."""
    out = []
    for p in (parts or []):
        inl = getattr(p, "inline_data", None)
        if inl is not None:
            mime = (getattr(inl, "mime_type", "") or "").lower()
            if not mime.startswith(_ECHO_SAFE_INLINE_PREFIXES):
                log.info(
                    f"/ask: dropped un-echoable inline part ({mime!r}) "
                    f"from the model turn"
                )
                continue
        out.append(p)
    return out


def _json_safe(obj):
    """Recursively replace non-finite floats (NaN / ±Infinity) with None.

    Bare NaN is invalid JSON; the Gemini API rejects the whole request
    with 400 INVALID_ARGUMENT when a tool result carries one. Applied to
    every tool result in the loop so a single bad float can't kill an
    otherwise good answer."""
    import math as _math
    if isinstance(obj, float):
        return None if (_math.isnan(obj) or _math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


_QUERY_ROW_CAP = 500


_QUERY_TIMEOUT_S = 8.0


_QUERY_TEXT_CLAMP = 400


_SQL_WRITE_RE = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|"
    r"pragma|vacuum|reindex|trigger|grant|revoke|truncate)\b",
    re.IGNORECASE,
)


def _validate_select_sql(sql: str):
    """(ok, cleaned_sql_or_error). Read-only, single SELECT/WITH only —
    the model-facing SQL surface, so the validation is strict AND the
    executor opens a mode=ro connection (defense in depth)."""
    if not sql or not sql.strip():
        return False, "empty query"
    s = sql.strip().rstrip(";").strip()
    if ";" in s:
        return False, "one statement only — no ';' inside the query"
    low = s.lower()
    if not (low.startswith("select") or low.startswith("with")):
        return False, "only SELECT / WITH queries are allowed (read-only)"
    if _SQL_WRITE_RE.search(s):
        return False, (
            "read-only: write/DDL/PRAGMA/ATTACH keywords are blocked"
        )
    return True, s


async def _execute_query_data(args: dict) -> dict:
    """Run a read-only SELECT against the SQLite DB and return rows.
    Read-only connection + validation + row cap + timeout + text clamp."""
    ok, s = _validate_select_sql(args.get("sql") or "")
    if not ok:
        return {"status": "error", "error": s}
    capped = (
        s if re.search(r"\blimit\b", s, re.IGNORECASE)
        else f"{s} LIMIT {_QUERY_ROW_CAP}"
    )

    def _run():
        import sqlite3 as _sql
        import time as _t
        try:
            conn = _sql.connect(
                f"file:{settings.db_path}?mode=ro", uri=True, timeout=3
            )
        except Exception as e:
            return {"status": "error",
                    "error": f"cannot open db read-only: {e}"}
        conn.row_factory = _sql.Row
        _deadline = _t.monotonic() + _QUERY_TIMEOUT_S
        conn.set_progress_handler(
            lambda: 1 if _t.monotonic() > _deadline else 0, 20000
        )
        try:
            cur = conn.execute(capped)
            rows = cur.fetchmany(_QUERY_ROW_CAP)
            cols = [d[0] for d in cur.description] if cur.description else []
            data = [dict(r) for r in rows]
            for r in data:  # clamp wide text so SELECT * can't blow context
                for k, v in list(r.items()):
                    if isinstance(v, str) and len(v) > _QUERY_TEXT_CLAMP:
                        r[k] = v[:_QUERY_TEXT_CLAMP] + "…"
            return {
                "status": "ok",
                "columns": cols,
                "rows": data,
                "row_count": len(data),
                "truncated": len(data) >= _QUERY_ROW_CAP,
            }
        except Exception as e:
            return {"status": "error",
                    "error": f"{type(e).__name__}: {str(e)[:200]}"}
        finally:
            conn.close()

    return await asyncio.to_thread(_run)


def _build_query_data_tool():
    """FunctionDeclaration for `query_data` — read-only SQL over the
    bot's SQLite DB, for aggregate/time-series analysis the other tools
    can't do (they return capped individual rows, not GROUP BY counts)."""
    from google.genai import types
    return types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="query_data",
                description=_TOOL_DOCS["query_data"] + (
                    "Run a READ-ONLY SQL SELECT against the bot's SQLite "
                    "database and get rows back — for aggregates, "
                    "trends-over-time, activity-by-hour, group-bys, and "
                    "any analysis the other tools can't do (they return "
                    "capped individual rows, not counts). Pair it with "
                    "code execution: query the aggregate, then chart it. "
                    "SELECT / WITH only; writes, DDL, PRAGMA, ATTACH and "
                    "multi-statement are blocked; results cap at 500 "
                    "rows and wide text fields are truncated.\n\n"
                    "**DON'T PROBE THE SCHEMA — it's fully documented "
                    "below.** `PRAGMA` is blocked and "
                    "sqlite_master/SELECT * round-trips burn your tool "
                    "budget before you get to the actual analysis "
                    "(observed 2026-07-29: three of six rounds spent on "
                    "discovery). Write the real query first time.\n\n"
                    "TABLES (columns):\n"
                    "- **latest_pdf_analyses** (VIEW, ~13K rows — USE "
                    "THIS for institutional-research questions, never "
                    "raw pdf_analyses): analysis_id, pdf_file_id, "
                    "source ('Goldman Sachs', 'JPMorgan', 'UBS'...), "
                    "report_type ('macro','equity_research',"
                    "'morning_briefing','vol_commentary','crypto'...), "
                    "title, priority ('high'/'medium'/'low'), "
                    "file_name, published_at (ISO date the PDF landed), "
                    "analyzed_at, analysis_json. Already deduped to the "
                    "LATEST analysis per PDF and joined to the file "
                    "row — raw pdf_analyses is append-only and will "
                    "double-count reanalyzed PDFs.\n"
                    "- **trade_scoreboard** (VIEW — USE THIS for ANY "
                    "win-rate / performance question, never hand-roll "
                    "it from analyst_trades): trader_key, trader, "
                    "logged_trades, "
                    "documented_wins, documented_losses, "
                    "closed_unscored, never_closed, "
                    "win_rate_BIASED_documented_only, "
                    "win_rate_closed_positions_only, "
                    "win_rate_honest_ghosts_as_losses, "
                    "avg_gain_on_wins_only. The ledger is WINS-BIASED — "
                    "gain_pct exists only where someone posted a close, "
                    "and members screenshot winners while abandoning "
                    "losers — so wins/COUNT(gain_pct) prints fake "
                    "96-100% win rates. THREE rates bracket the truth; "
                    "DEFAULT TO win_rate_closed_positions_only AND "
                    "ALWAYS say the never_closed count next to it "
                    "('X% on N closed positions, with M more opened and "
                    "never closed out'). The BIASED one counts only "
                    "exits posted WITH a number; the ghosts_as_losses "
                    "one calls a position opened this morning a loss. "
                    "Cite either of those only if you say what it does "
                    "to the denominator. NEVER GROUP analyst_trades BY author OR "
                    "caller — this room renames constantly and one "
                    "person posts under many display names (author_id "
                    "423994649317736448 = 'BK' + 'M&AK' + "
                    "'bearishkyle'; 1192771108332650496 = 'abe' + "
                    "'abugs bunny' + 'abullish_xyz' + 'abearish'). "
                    "Name-grouping split one trader's 184 trades into "
                    "81/73/21. Group by author_id, or just use the "
                    "view, which already does.\n"
                    "- **analyst_trades** is 32.9K rows but only ~887 "
                    "have is_trade=1 — the rest are messages the "
                    "extractor read and correctly judged not to be "
                    "trades. ALWAYS filter is_trade=1; a raw COUNT(*) "
                    "overstates activity ~37x. The ledger starts "
                    "2026-05-11, so 'all-time' is only ~3 months.\n"
                    "- closed_unscored vs never_closed are DIFFERENT: "
                    "closed_unscored = the member posted an exit with no "
                    "percentage in it ('sold DELL way too early smh') so "
                    "it can't be scored; never_closed = they announced "
                    "an entry and never showed any exit. Don't describe "
                    "an unscored close as an open position.\n"
                    "- **pdf_entities** (ticker index over the research): "
                    "analysis_id, pdf_file_id, ticker, name, "
                    "asset_class. Join to latest_pdf_analyses on "
                    "analysis_id for 'which banks mentioned $NVDA' — "
                    "indexed, so prefer it over json_each on "
                    "analysis_json.\n"
                    "- analysis_json (in the view) still holds the deep "
                    "fields: key_insights[], market_movers[] "
                    "({ticker,action,rating,price_target,conviction,"
                    "rationale}), trade_ideas[], sector_views[], "
                    "macro_indicators[], key_data_points[], "
                    "theme_stances[], risk_factors[]. Reach into them "
                    "with json_extract / json_each when the columns "
                    "above aren't enough.\n"
                    "- chat_messages (174K rows): id, channel_name, "
                    "author_id (the STABLE identity key — per-member "
                    "GROUP BYs use author_id, never a name; renames "
                    "split one person across many display names), "
                    "author_username, author_display, content, posted_at "
                    "(ISO-8601 TEXT), reply_parent_id, image_ocr_text. "
                    "The full chat corpus — use for activity/trend/"
                    "over-time analysis and for scoring/ranking members "
                    "by a trait (label results with MAX(author_display) "
                    "per author_id). There is NO precomputed racism "
                    "score per message; approximate with LIKE on content "
                    "(e.g. content LIKE '%nigg%') for a rough slur trend, "
                    "and SAY it's an approximation.\n"
                    "- analyst_trades (32.9K rows, ~887 real — see "
                    "above): author_username via "
                    "`author`, author_id, caller, ticker, contract_type "
                    "('call'/'put'), strike, expiry, action "
                    "('open'/'add'/'close'/'trim'), gain_pct (ONLY on "
                    "documented closes/trims), inferred_status "
                    "('expired_unknown' = a ghost: opened, never closed), "
                    "posted_at, price. WINS-BIASED — members screenshot "
                    "winners; losses leak as ghosts — so a naive "
                    "COUNT(gain_pct>0)/COUNT(*) is NOT a true win rate; "
                    "note the bias.\n"
                    "- user_profiles (55 rows, one per user): user_id, "
                    "username, display_name, trader_score, trader_rank, "
                    "racial_humor_score (0-100), slur_count, "
                    "message_count_at_update, updated_at.\n"
                    "- user_metrics (54 rows): user_id, slur_count_30d, "
                    "total_messages_30d, trader_score, trader_rank "
                    "(current snapshot).\n"
                    "- daily_reports (104 rows): report_date, "
                    "report_type ('daily'/'manual'), pdf_count, "
                    "created_at.\n\n"
                    "NOTES: posted_at/created_at are ISO TEXT — compare "
                    "with date()/datetime(); some rows mix 'T' and space "
                    "separators, so wrap in datetime() to be safe. Bucket "
                    "time with strftime('%Y-%W', posted_at) for weekly, "
                    "strftime('%Y-%m-%d', ...) for daily, "
                    "strftime('%H', ...) for hour-of-day."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "sql": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "A single read-only SELECT/WITH query. "
                                "GROUP BY / aggregates encouraged; a "
                                "LIMIT is auto-added if you omit one."
                            ),
                        ),
                    },
                    required=["sql"],
                ),
            )
        ]
    )


def _build_earnings_slate_tool():
    """Tool: a date's full earnings slate, cap-ranked (2026-09-01)."""
    from google.genai import types
    return types.Tool(function_declarations=[
            types.FunctionDeclaration(
                name="lookup_earnings_slate",
                description=_TOOL_DOCS["lookup_earnings_slate"] + (
                    "\n\nResponse shape: {status, date, before_open: "
                    "[{symbol, name, market_cap_musd, "
                    "session_confirmed}], after_close: [...], counts}. "
                    "status: ok | empty (no US earnings that date) | "
                    "error (feed down — say so, do NOT substitute a "
                    "Google list)."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "date": types.Schema(
                            type=types.Type.STRING,
                            description=("'YYYY-MM-DD' or 'tomorrow'; "
                                         "omit for today (ET)"),
                        ),
                    },
                ),
            ),
    ])


def _build_earnings_date_tool():
    """FunctionDeclaration for `lookup_earnings_date`. Per-symbol
    earnings dates from Finnhub's `/calendar/earnings` endpoint. The
    pulse's earnings block is whitelist-filtered (MAG7 / big banks /
    bellwethers — noise control for a broadcast), so /ask had NO data
    source for "when does GEO report next" on a non-whitelist ticker
    and the model dodged the actual question with adjacent facts
    (observed 2026-06-10 19:10 UTC). A user naming a specific ticker
    IS the filter — no whitelist on this tool.
    """
    from google.genai import types
    return types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="lookup_earnings_date",
                description=_TOOL_DOCS["lookup_earnings_date"] + (
                    "Next upcoming earnings date + last reported "
                    "quarter for ONE stock ticker. Returns the next "
                    "report date with timing (before open / after "
                    "close) and EPS/revenue estimates if posted, plus "
                    "the most recent reported quarter's EPS actual vs "
                    "estimate. Works for ANY US-listed ticker — no "
                    "whitelist.\n\n"
                    "USE for: 'when does GEO report', 'NVDA earnings "
                    "date', 'when is SMCI's next quarter', 'did PLTR "
                    "beat last quarter', 'what's expected for AVGO "
                    "earnings'.\n\n"
                    "DO NOT use for: earnings CONTENT questions "
                    "(guidance commentary, call takeaways, why the "
                    "stock moved post-print — Google Search), macro "
                    "data prints (lookup_economic_calendar), or "
                    "broad 'what reports this week' sweeps (Google "
                    "Search — this tool is one symbol at a time).\n\n"
                    "Args:\n"
                    "  symbol: ticker, e.g. 'GEO', 'NVDA', 'BRK.B'.\n\n"
                    "Response shape: {status, symbol, next: {date, "
                    "timing, eps_estimate, revenue_estimate} | null, "
                    "last: {date, eps_actual, eps_estimate} | null, "
                    "as_of}. `next` null = no confirmed upcoming date "
                    "on the calendar yet (common >6 weeks out — fall "
                    "back to Google Search for the company's announced "
                    "or historically-typical reporting window, and say "
                    "whether the date is confirmed or estimated)."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "symbol": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "Stock ticker (e.g. 'GEO', 'NVDA'). "
                                "One symbol per call."
                            ),
                        ),
                    },
                    required=["symbol"],
                ),
            )
        ]
    )


async def _execute_earnings_slate(args: dict) -> dict:
    """Today's (or a given date's) FULL earnings slate, cap-ranked.

    Built 2026-09-01 after the room asked "who reports after close
    today" three times in one afternoon and got three different
    partial answers, each missing PANW — the largest name on the
    slate. Every one of those turns called NO tool: the earnings-date
    tool's own docs sent broad "what reports today" sweeps to Google
    Search, and a search snippet is a partial list by nature (one
    answer was sourced to digrin.com).

    Same Finnhub feed and the same cap ranking the calendar graphic
    uses, so the bot and the sheet can no longer disagree about who
    reports.
    """
    from datetime import datetime, timedelta

    import pytz

    date_iso = (args.get("date") or "").strip()
    if not date_iso:
        et = datetime.now(pytz.timezone(settings.timezone))
        date_iso = et.strftime("%Y-%m-%d")
    elif date_iso.lower() == "tomorrow":
        et = datetime.now(pytz.timezone(settings.timezone)) + timedelta(days=1)
        date_iso = et.strftime("%Y-%m-%d")

    as_of = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        from report import calendar_data as _cd
        from report import news_data as _nd

        raw = await asyncio.to_thread(_nd.fetch_earnings_calendar_all,
                                      date_iso)
        if raw is None:
            return {"status": "error", "as_of": as_of, "date": date_iso,
                    "error": "earnings feed unavailable right now"}
        bmo, amc, conf, seen = [], [], {}, set()
        for r in raw:
            sym = (r.get("symbol") or "").strip()
            if not sym or sym in seen:
                continue
            seen.add(sym)
            hour = (r.get("hour") or "").lower()
            if hour == "bmo":
                bmo.append(sym)
                conf[sym] = True
            else:
                amc.append(sym)
                conf[sym] = hour == "amc"
        if not bmo and not amc:
            return {"status": "empty", "as_of": as_of, "date": date_iso,
                    "note": "no US earnings scheduled for this date"}

        caps = await asyncio.to_thread(_cd._resolve_caps, bmo + amc)

        def _rows(syms):
            ranked = sorted(
                syms, key=lambda s: -(caps.get(s, {}).get("cap") or 0))
            return [{
                "symbol": s,
                "name": str(caps.get(s, {}).get("name") or s),
                "market_cap_musd": round(
                    float(caps.get(s, {}).get("cap") or 0)),
                # Finnhub fills `hour` progressively through the day; an
                # unconfirmed session is NOT absence from the slate, and
                # saying so is what keeps a big name from vanishing.
                "session_confirmed": bool(conf.get(s)),
            } for s in ranked[:25]]

        return {
            "status": "ok", "as_of": as_of, "date": date_iso,
            "before_open": _rows(bmo), "after_close": _rows(amc),
            "counts": {"before_open": len(bmo), "after_close": len(amc)},
            "note": ("Cap-ranked, top 25 per session. "
                     "session_confirmed=false means Finnhub has not yet "
                     "stamped the timing, not that the company is absent "
                     "— report those names, noting the session is "
                     "unconfirmed."),
        }
    except Exception as e:
        log.warning(f"lookup_earnings_slate failed: {e}")
        return {"status": "error", "as_of": as_of, "date": date_iso,
                "error": f"{type(e).__name__}: {e}"}


async def _execute_earnings_date(args: dict) -> dict:
    """Run the lookup_earnings_date tool call.

    Distinguishes no-data (status='no_data' — ticker valid but no
    calendar rows; model should fall back to Google Search and answer
    the actual date question) from fetch failure (status='error' —
    same fallback). Both payloads tell the model explicitly: Google is
    the correct next step, and the answer must address the DATE the
    asker asked for — not dodge into adjacent facts about the company.
    """
    from datetime import datetime
    from report import news_data as _nd

    symbol = (args.get("symbol") or "").strip().upper()
    if not symbol:
        return {
            "status": "error",
            "error": "No symbol provided — re-call with a ticker.",
        }

    try:
        # to_thread: synchronous urllib I/O — keep it off the event loop.
        result = await asyncio.to_thread(
            _nd.fetch_earnings_date_for_symbol, symbol,
        )
    except Exception as e:
        log.warning(f"lookup_earnings_date: fetcher raised: {e}")
        result = None

    if result is None:
        return {
            "status": "error",
            "symbol": symbol,
            "error": (
                "Finnhub earnings-calendar fetch failed. FALL BACK TO "
                "GOOGLE SEARCH now and answer the asker's actual "
                "question (the report date) — do not substitute "
                "adjacent facts about the company. Say whether the "
                "date you find is company-confirmed or estimated."
            ),
        }

    if not result.get("next") and not result.get("last"):
        return {
            "status": "no_data",
            "symbol": symbol,
            "error": (
                f"No earnings-calendar rows for {symbol} in the "
                f"-30d/+120d window (unconfirmed date, foreign "
                f"listing, or unrecognized ticker). FALL BACK TO "
                f"GOOGLE SEARCH now and answer the actual date "
                f"question — flag whether the date is confirmed or "
                f"an estimate. Do not dodge into adjacent facts."
            ),
        }

    return {
        "status": "ok",
        **result,
        "as_of": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    }


def _build_options_chain_tool():
    """FunctionDeclaration for `lookup_options_chain`. Returns aggregated
    options-chain stats (total call/put volume + OI, ATM IV, put-call
    ratios) for ONE expiration. Without `expiration`, returns the
    nearest expiration's summary plus the list of available expirations
    so the model can re-call for a further-out one.

    Sourced from Yahoo's v7 options endpoint (free, public, no auth).
    Yahoo rate-limits datacenter IPs intermittently — fetch failures
    return `{"status": "error", ...}` and the model should tell the
    asker the chain isn't available rather than inventing numbers.
    """
    from google.genai import types
    return types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="lookup_options_chain",
                description=_TOOL_DOCS["lookup_options_chain"] + (
                    "Aggregated options-chain stats for ONE expiration "
                    "of a stock / ETF / index. Returns total call + put "
                    "volume, total call + put open interest, ATM "
                    "implied volatility, put-call ratios (volume + OI), "
                    "and the list of available expirations.\n\n"
                    "USE for: 'what's the OI on SPY next week', 'NVDA "
                    "options volume for the June 12 expiration', 'put-"
                    "call ratio on QQQ', 'IV on SPY this Friday', AND "
                    "single-strike questions — pass `strike` + "
                    "`contract_type` for one contract's CURRENT OI / "
                    "volume / IV ('what's the OI on MSFT 400c 7/31', "
                    "'IV on the SPY 750 calls'). Snapshot only — there "
                    "is NO multi-day history, so a '5-day OI trend' "
                    "isn't available (say so; don't fabricate it).\n\n"
                    "Args:\n"
                    "  symbol: ticker (SPY, QQQ, NVDA, NDX, SPX, etc.)\n"
                    "  expiration: optional ISO date 'YYYY-MM-DD'. When "
                    "omitted, returns the NEAREST expiration + the "
                    "list of available expirations so you can re-call "
                    "for a further-out one if the asker meant 'next "
                    "week' / 'this Friday' / a specific date.\n"
                    "  strike: optional number. When set, returns that "
                    "ONE contract's stats instead of the aggregate.\n"
                    "  contract_type: 'call' or 'put' (default 'call'); "
                    "used with strike to pick the side.\n\n"
                    "Response carries `status` field: 'ok' / 'no_chain' "
                    "(Yahoo returned nothing — chain may not exist for "
                    "this symbol) / 'error' (fetch failed, rate-limit "
                    "or upstream issue). On 'no_chain' or 'error', "
                    "tell the asker the data isn't available — do NOT "
                    "invent OI / IV / volume numbers."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "symbol": types.Schema(
                            type=types.Type.STRING,
                            description="Ticker, e.g. 'SPY' or 'NDX'.",
                        ),
                        "expiration": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "Optional ISO date 'YYYY-MM-DD'. Omit "
                                "to get the nearest expiration's "
                                "summary + list of available dates."
                            ),
                        ),
                        "strike": types.Schema(
                            type=types.Type.NUMBER,
                            description=(
                                "Optional strike price. When set, "
                                "returns that ONE contract's current "
                                "OI / volume / IV instead of the "
                                "expiration aggregate."
                            ),
                        ),
                        "contract_type": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "'call' or 'put' (default 'call'). "
                                "Used with `strike` to pick the side."
                            ),
                        ),
                    },
                    required=["symbol"],
                ),
            )
        ]
    )


async def _execute_options_chain(args: dict) -> dict:
    """Run the lookup_options_chain tool call.

    Fetches via yfinance (which handles Yahoo's session-crumb gate),
    summarizes via market_data.summarize_options_chain. When
    `expiration` is provided as 'YYYY-MM-DD', resolves to the
    matching available expiration (yfinance returns ISO strings
    directly, no unix conversion needed). Returns an LLM-ready
    dict with status / summary / available_expirations.
    """
    from datetime import datetime
    from report import market_data as _md

    symbol = (args.get("symbol") or "").strip().upper()
    if not symbol:
        return {"status": "error", "error": "symbol is required"}

    expiration_iso = (args.get("expiration") or "").strip()

    # Validate ISO date shape BEFORE the fetch so we can return a clean
    # error without spending a Yahoo call. yfinance is generally tolerant
    # of available_expirations being populated from a separate fetch but
    # we want fast-fail on bad input.
    if expiration_iso:
        try:
            datetime.strptime(expiration_iso, "%Y-%m-%d")
        except ValueError:
            # Get the available list so the model can re-call cleanly.
            # to_thread: yfinance does sync HTTP — never on the event loop.
            raw0 = await asyncio.to_thread(
                _md._fetch_yahoo_options_chain, symbol
            )
            return {
                "status": "error",
                "error": (
                    f"expiration {expiration_iso!r} is not ISO date "
                    f"'YYYY-MM-DD'. Available expirations follow."
                ),
                "available_expirations": (
                    (raw0 or {}).get("expiration_dates", [])[:12]
                ),
            }

    # to_thread: yfinance fetch = multiple sync HTTP round-trips (options
    # list + chain + fast_info). Blocking the event loop here froze the
    # whole bot for the fetch duration (2026-06-10 second-pass review).
    raw = await asyncio.to_thread(
        _md._fetch_yahoo_options_chain,
        symbol,
        expiration_iso=(expiration_iso or None),
    )
    if raw is None:
        return {
            "status": "error",
            "error": (
                f"yfinance options-chain fetch failed for {symbol} "
                f"(rate-limit or upstream issue). No live data — tell "
                f"the asker to check their broker."
            ),
        }

    expirations = raw.get("expiration_dates") or []
    if not expirations:
        return {
            "status": "no_chain",
            "symbol": symbol,
            "error": (
                f"yfinance returned no options expirations for {symbol}. "
                f"This symbol may not have listed options chains."
            ),
        }

    # Per-strike path (2026-07-29): current OI/volume/IV for ONE
    # contract. The chain we already fetched carries every strike; when
    # the asker names a specific strike we filter to it instead of only
    # returning the expiration aggregate. Snapshot only — no history.
    strike_raw = args.get("strike")
    if strike_raw not in (None, ""):
        try:
            want = float(strike_raw)
        except (TypeError, ValueError):
            want = None
        ctype = str(args.get("contract_type") or "call").strip().lower()
        side = "puts" if ctype in ("put", "puts", "p") else "calls"
        chain = raw.get("chain") or {}
        contracts = chain.get(side) or []
        avail = sorted({c.get("strike") for c in contracts
                        if c.get("strike") is not None})
        match = None
        if want is not None:
            match = next(
                (c for c in contracts
                 if c.get("strike") is not None
                 and abs(float(c["strike"]) - want) < 1e-6),
                None,
            )
        if match is None:
            return {
                "status": "no_strike",
                "symbol": symbol,
                "contract_type": "put" if side == "puts" else "call",
                "expiration_iso": chain.get("expiration_iso"),
                "error": (
                    f"no {('put' if side == 'puts' else 'call')} at strike "
                    f"{strike_raw} on {symbol} {chain.get('expiration_iso')}"
                ),
                "available_strikes": avail[:40],
                "as_of": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
            }
        return {
            "status": "ok",
            "symbol": symbol,
            "contract": {
                "strike": match.get("strike"),
                "contract_type": "put" if side == "puts" else "call",
                "expiration_iso": chain.get("expiration_iso"),
                "open_interest": match.get("openInterest"),
                "volume": match.get("volume"),
                "implied_volatility": match.get("impliedVolatility"),
                "bid": match.get("bid"),
                "ask": match.get("ask"),
                "last_price": match.get("lastPrice"),
                "underlying_spot_price": raw.get("underlying_spot_price"),
            },
            "history_note": (
                "SNAPSHOT ONLY — this is the current OI/volume/IV. No "
                "multi-day history is available; for a 5-day OI trend "
                "tell the asker to pull it from their broker."
            ),
            "available_expirations": expirations[:12],
            "as_of": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        }

    summary = _md.summarize_options_chain(raw)
    return {
        "status": "ok",
        "summary": summary,
        "available_expirations": expirations[:12],
        "as_of": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    }


def _build_fantasy_league_tool():
    """FunctionDeclaration for `lookup_fantasy_league` — live data from
    the room's Sleeper league (settings.sleeper_league_id). Registered
    only when the league id is configured."""
    from google.genai import types
    return types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="lookup_fantasy_league",
                description=_TOOL_DOCS["lookup_fantasy_league"] + (
                    "Live data from the room's Sleeper fantasy football "
                    "league (Omnibeta Degens). Use for ANY question "
                    "about the fantasy league: standings, records, "
                    "matchup scores, rosters, waiver/trade activity, "
                    "draft picks, who's trending, projections. Managers "
                    "are resolved to their Discord identities.\n"
                    "`topic` (required): 'league' (settings + who's in "
                    "it) | 'standings' (records + points) | 'matchups' "
                    "(scores for a week) | 'roster' (one manager's "
                    "starters + bench — requires `member`) | "
                    "'transactions' (waivers/trades/FAAB, recent) | "
                    "'draft' (all picks + rosters_by_manager; USE THIS for draft grading/review/who-drafted-best questions, NOT standings, which is all zeros pre-season) | 'trending' (adds/drops across "
                    "all of Sleeper) | 'projections' (projected PPR "
                    "points; optional `member` for their starters).\n"
                    "`week`: NFL week number (defaults to the current "
                    "week).\n"
                    "`member`: a Discord username/display name or "
                    "Sleeper name, for topic='roster' or "
                    "'projections'."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "topic": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "league | standings | matchups | roster "
                                "| transactions | draft | trending | "
                                "projections"
                            ),
                        ),
                        "week": types.Schema(
                            type=types.Type.INTEGER,
                            description="NFL week (default: current).",
                        ),
                        "member": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "Manager to look up (discord or sleeper "
                                "name) — for roster/projections."
                            ),
                        ),
                    },
                    required=["topic"],
                ),
            )
        ]
    )


async def _execute_fantasy_league(args: dict) -> dict:
    """Run the lookup_fantasy_league tool call. All Sleeper I/O is
    synchronous urllib in report/sleeper_data — run in a thread. Player
    IDs translate through the sleeper_players DB cache, lazily refreshed
    on first use / when older than 26h (the daily scheduler job is the
    primary refresher; this is the backstop)."""
    from report import sleeper_data as _sd

    league_id = (settings.sleeper_league_id or "").strip()
    if not league_id:
        return {
            "status": "error",
            "error": (
                "fantasy league not configured (SLEEPER_LEAGUE_ID unset) "
                "— say the fantasy lookup isn't available."
            ),
        }

    def _sync() -> dict:
        age = db.sleeper_players_cache_age_hours()
        if age is None or age > 26:
            try:
                n = db.upsert_sleeper_players(_sd.fetch_players_trimmed())
                log.info(f"sleeper players cache refreshed ({n} rows)")
            except Exception as e:
                # stale cache still translates most ids; empty cache
                # degrades to raw ids, which the payload surfaces
                log.warning(f"sleeper players cache refresh failed: {e}")
        return _sd.build_topic_payload(
            league_id,
            (args.get("topic") or "standings"),
            week=args.get("week"),
            member=(args.get("member") or "").strip() or None,
            player_name_resolver=db.get_sleeper_player_names,
        )

    try:
        result = await asyncio.to_thread(_sync)
    except Exception as e:
        log.warning(f"lookup_fantasy_league failed: {e}")
        return {
            "status": "error",
            "error": (
                f"Sleeper API unavailable ({str(e)[:120]}) — say the "
                "lookup failed. Do NOT invent league data."
            ),
        }
    return result


def _build_market_price_tool():
    """FunctionDeclaration for `lookup_market_price`. Routes symbols
    to Finnhub (stocks) or Binance.US (crypto) based on a hardcoded
    allowlist. Returns a session-labeled snapshot."""
    from google.genai import types
    return types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="lookup_market_price",
                description=_TOOL_DOCS["lookup_market_price"] + (
                    "Get prices for stocks / ETFs / indices and crypto. "
                    "Pass a list of symbols (cap 10 per call). Response "
                    "includes per-symbol price, change_pct, source, "
                    "data_freshness, plus a session label ('OPEN' | "
                    "'PRE-MARKET' | 'AFTER-HOURS' | 'WEEKEND-CLOSED').\n\n"
                    "DATA FRESHNESS PER SYMBOL — check `data_freshness` "
                    "on each quote before phrasing the move:\n"
                    "  - 'live_regular_session' — OPEN-session live "
                    "Finnhub price. Describe as 'session-to-date' or "
                    "'right now'.\n"
                    "  - 'live_extended_hours' — Yahoo extended-hours "
                    "print. `price` is the actual last AH/PRE trade; "
                    "`change_pct` is from PRIOR-day close (full move "
                    "incl. AH); `extended_hours_change_pct` is the AH "
                    "move from today's regular close; "
                    "`regular_session_close` is today's 4 PM close. "
                    "Describe as 'after-hours at $X (closed $Y, then "
                    "moved Z% in AH)'.\n"
                    "  - 'regular_session_close' — Finnhub fallback "
                    "when Yahoo AH/PRE data was unavailable. `price` "
                    "is the 4 PM close. Tell the asker the AH/PRE "
                    "print is not in your feed; quote the cash close "
                    "as a reference. Do NOT phrase as 'after-hours "
                    "at $X' — it's the 4 PM close.\n\n"
                    "`stock_quote_data_caveat` on the top-level "
                    "response will flag if any stock fell back to the "
                    "regular close. None when every stock has live "
                    "extended-hours data or session is OPEN.\n\n"
                    "Crypto IS live 24/7 regardless of session - "
                    "phrase BTC/ETH normally.\n\n"
                    "Use for 'what's TSLA at', 'how's BTC doing', "
                    "'is SPY green today', 'GTLB after earnings'."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "symbols": types.Schema(
                            type=types.Type.ARRAY,
                            items=types.Schema(type=types.Type.STRING),
                            description="Symbol tickers, e.g. ['TSLA', 'BTC'].",
                        ),
                    },
                ),
            )
        ]
    )


async def _execute_market_price(args: dict) -> dict:
    """Run the lookup_market_price tool call.

    Routes each symbol to Finnhub or Binance.US, collects per-symbol
    responses, prepends a session label so the model can phrase
    correctly. Per-symbol failures don't sink the batch.
    """
    from datetime import datetime
    import pytz
    from report import market_data as _md

    symbols_in = args.get("symbols")
    if not symbols_in or not isinstance(symbols_in, list):
        return {"error": "symbols list cannot be empty"}

    symbols = [str(s).strip().upper() for s in symbols_in if isinstance(s, str) and str(s).strip()]
    if not symbols:
        return {"error": "symbols list cannot be empty"}

    truncated_to = None
    if len(symbols) > 10:
        symbols = symbols[:10]
        truncated_to = 10

    # Session label from existing market_data helper. The helper returns
    # BOTH a short code AND an explanatory note — previously we threw the
    # note away (`_note`) and only kept the code. That meant Gemini saw
    # session=AFTER-HOURS + a price but no warning that the Finnhub /quote
    # response on AH/PRE is the regular-session CLOSE, not a live extended-
    # hours print. Wock asked about TSLA/GTLB after-hours on 2026-06-02
    # and got the 4 PM close quoted as if it were the live AH print. The
    # note now flows through to the tool response + an AH/PRE-specific
    # data-quality warning gets appended so the model phrases correctly.
    et = pytz.timezone("America/New_York")
    now_et = datetime.utcnow().replace(tzinfo=pytz.UTC).astimezone(et)
    try:
        session_code, session_note = _md._session_label(now_et)
    except Exception:
        session_code = "UNKNOWN"
        session_note = ""

    # Stock-quote data-quality caveat for sessions where Finnhub /quote
    # does NOT return live extended-hours data. The price field for a
    # stock on AFTER-HOURS / PRE-MARKET / WEEKEND-CLOSED is the most
    # recent REGULAR-session close, not the live extended-hours print.
    # Crypto via Binance.US is always live so this caveat doesn't apply
    # to it — the model should still phrase BTC/ETH as 'right now'.
    quote_data_caveat = ""
    if session_code == "AFTER-HOURS":
        quote_data_caveat = (
            "STOCK PRICES BELOW = today's regular-session CLOSE (4 PM ET). "
            "Finnhub /quote does NOT return live after-hours prints. If the "
            "asker explicitly wants after-hours movement (e.g. on an earnings "
            "name like GTLB / NVDA / MSFT that reported AMC), tell them the "
            "AH print is not in your data feed and quote the cash close as "
            "a reference point. Don't phrase the price as 'after-hours at $X' "
            "— it's the 4 PM close. Crypto prices ARE live."
        )
    elif session_code == "PRE-MARKET":
        quote_data_caveat = (
            "STOCK PRICES BELOW = YESTERDAY'S regular-session close. Finnhub "
            "/quote does NOT return live pre-market prints. Phrase as "
            "'yesterday's close' not 'pre-market price at $X'. Crypto prices "
            "ARE live."
        )
    elif session_code == "WEEKEND-CLOSED":
        quote_data_caveat = (
            "STOCK PRICES BELOW = FRIDAY'S regular-session close. Markets "
            "are closed for the weekend. Crypto prices ARE live."
        )

    timestamp = now_et.strftime("%Y-%m-%d %H:%M %Z")

    quotes: list[dict] = []
    for sym in symbols:
        if sym in _CRYPTO_SYMBOLS:
            cq = await _crypto_quote(sym)
            if cq:
                quotes.append(cq)
            else:
                quotes.append({
                    "symbol": sym,
                    "error": f"no live feed for {sym} (Binance.US quote "
                             f"unavailable right now)",
                })
        else:
            # During AFTER-HOURS / PRE-MARKET, try Yahoo first — it
            # surfaces the actual extended-hours print via the v8
            # chart endpoint. Yahoo can rate-limit datacenter IPs
            # intermittently; fall back to Finnhub on any failure or
            # if Yahoo's reported session doesn't match what we expect.
            yh = None
            expected_yh_session = (
                "post" if session_code == "AFTER-HOURS"
                else "pre" if session_code == "PRE-MARKET"
                else None
            )
            if expected_yh_session:
                try:
                    # to_thread: sync urllib I/O — never on the event loop.
                    yh = await asyncio.to_thread(
                        _md._fetch_yahoo_extended_hours, sym
                    )
                except Exception as e:
                    log.info(f"yahoo AH lookup for {sym} raised: {e}")
                    yh = None

            # Use Yahoo's extended-hours print iff it actually returned
            # a bar from the expected session. Otherwise fall through
            # to Finnhub (regular-session close).
            if (
                yh
                and yh.get("last_session") == expected_yh_session
                and yh.get("last_price") is not None
                and yh.get("regular_close")
                and yh.get("prev_close")
            ):
                last_price = float(yh["last_price"])
                regular_close = float(yh["regular_close"])
                prev_close = float(yh["prev_close"])
                # change_pct vs prior REGULAR close (so the asker
                # sees the full day-over-day move including the AH
                # action). Also surface ah_change_pct = AH move from
                # the regular close so the model can describe both:
                # "GTLB closed +X% then dropped Y% after-hours."
                change_pct = (
                    (last_price - prev_close) / prev_close * 100.0
                    if prev_close
                    else None
                )
                ah_change_pct = (
                    (last_price - regular_close) / regular_close * 100.0
                    if regular_close
                    else None
                )
                quotes.append({
                    "symbol": sym,
                    "price": last_price,
                    "change_pct": change_pct,
                    "prev_close": prev_close,
                    "regular_session_close": regular_close,
                    "extended_hours_change_pct": ah_change_pct,
                    "source": "yahoo_extended_hours",
                    "data_freshness": "live_extended_hours",
                })
                continue

            try:
                # to_thread: sync urllib I/O — never on the event loop.
                data = await asyncio.to_thread(_md._fetch_finnhub_quote, sym)
            except Exception as e:
                data = None
                log.info(f"finnhub quote for {sym} raised: {e}")
            if not data:
                # Not a resolvable US stock — try a Binance.US crypto
                # pair before giving up (dynamic crypto coverage: SUI,
                # PEPE, new listings that aren't in the priority set).
                cq = await _crypto_quote(sym)
                if cq:
                    quotes.append(cq)
                    continue
                quotes.append({
                    "symbol": sym,
                    "error": f"no live feed for {sym} — not a recognized "
                             f"US stock or Binance.US crypto pair",
                })
                continue
            quotes.append({
                "symbol": sym,
                "price": data.get("price"),
                "change_pct": data.get("change_pct"),
                "prev_close": data.get("prev_close"),
                "source": "finnhub",
                # During extended-hours sessions, Finnhub /quote is the
                # regular-session close (not live). Tag it so Gemini
                # can phrase correctly even when symbols mix sources.
                "data_freshness": (
                    "regular_session_close"
                    if session_code in ("AFTER-HOURS", "PRE-MARKET",
                                        "WEEKEND-CLOSED")
                    else "live_regular_session"
                ),
            })

    # Caveat applicability depends on how Yahoo did:
    #   all stocks got live AH/PRE data       -> drop the caveat
    #   all stocks fell back to Finnhub close -> keep the original caveat
    #   mixed (some live AH, some stale)      -> narrow caveat to stale ones
    if quote_data_caveat and quotes:
        stock_quotes = [q for q in quotes if q.get("source") != "binance"
                        and "error" not in q]
        if stock_quotes:
            live_ah_count = sum(
                1 for q in stock_quotes
                if q.get("data_freshness") == "live_extended_hours"
            )
            stale_symbols = [
                q["symbol"] for q in stock_quotes
                if q.get("data_freshness") == "regular_session_close"
            ]
            if live_ah_count == len(stock_quotes):
                # All stocks have live AH/PRE data — caveat doesn't apply.
                quote_data_caveat = None
            elif live_ah_count > 0 and stale_symbols:
                # Mixed — narrow the caveat to the stale ones.
                quote_data_caveat = (
                    f"PARTIAL DATA: {','.join(stale_symbols)} fell back to "
                    f"Finnhub regular-session close (Yahoo AH/PRE data was "
                    f"unavailable for those tickers). Other tickers have "
                    f"LIVE extended-hours prints. Per-symbol data_freshness "
                    f"field tells you which is which."
                )
            # else: all stocks stale -> keep original Fix A caveat unchanged

    result = {
        "session": session_code,
        "session_note": session_note,
        "stock_quote_data_caveat": quote_data_caveat or None,
        "timestamp": timestamp,
        "quotes": quotes,
    }
    if truncated_to is not None:
        result["truncated_to"] = truncated_to
    return result
