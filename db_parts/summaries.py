"""Cross-table summaries and reports that read several families at once.

Moved verbatim from db.py on 2026-09-01. Every reference to a db.py
function goes through `_db.<name>` so the facade stays the single
patch point and the thread-local connection model lives in db.py.
"""
import logging

import db as _db  # noqa: E402
from db import (  # noqa: E402
    _PROFILES_BLOCK_BUDGET_CHARS,
    _PROFILES_BLOCK_MAX_USERS,
    _PROFILE_SECTION_SPLIT_RE,
)

log = logging.getLogger("db")


# Backwards-compat alias — existing call sites pass no caller and expect
# the legacy (Abe-only-era) global tally. Going forward, prefer
# compute_caller_win_loss_summary(caller='abe') so the intent is explicit.
def compute_abe_win_loss_summary(days: int = 30) -> dict:
    return _db.compute_caller_win_loss_summary(days=days, caller="abe")


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
        rows = _db.get_recent_analyst_trades(
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
        positions = _db.get_current_analyst_positions(
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
        wl = _db.compute_caller_win_loss_summary(
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


def recompute_trader_ranks_on_profiles() -> None:
    """DEPRECATED — no-op kept for backward compat. trader_rank is
    computed on-read via get_global_trader_ranks() now. The
    user_profiles.trader_rank column is dead storage; ignore values
    you see there from older deploys.
    """
    return  # no-op


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
    meta: dict | None = None,
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

        # Route/grounding/guard audit stamp (2026-07-09) — one line per
        # entry so QC reads decisions directly instead of inferring them
        # from the presence of Sources/hedges. Railway logs rotate away
        # in ~1 hour; this file is the durable audit record. Rendered
        # only when the caller passed meta (the full Gemini path).
        meta_line = ""
        if meta:
            try:
                bits = []
                if meta.get("route"):
                    bits.append(
                        f"`{meta['route']}/{meta.get('kind', '?')}`"
                    )
                if "grounded" in meta:
                    n_src = meta.get("sources") or 0
                    bits.append(
                        f"grounded ✅ ({n_src} source"
                        f"{'s' if n_src != 1 else ''})"
                        if meta["grounded"] else "ungrounded"
                    )
                if meta.get("ground_retry"):
                    bits.append(f"retry: {meta['ground_retry']}")
                if meta.get("filter_retry"):
                    bits.append(f"filter-retry: {meta['filter_retry']}")
                if meta.get("images"):
                    bits.append(f"images: {meta['images']}")
                guards = meta.get("guards") or []
                bits.append(
                    "guards: " + (", ".join(guards) if guards else "—")
                )
                meta_line = "**Route:** " + " · ".join(bits) + "\n\n"
            except Exception:
                meta_line = ""

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
            f"{meta_line}"
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


def _reorder_profile_for_roast_attention(text: str) -> str:
    """Injection-time section reorder (2026-07-10). Stored profiles put
    'Recent trades' ABOVE 'Recent personal life', so the model reads
    money-loss material before it ever reaches the personal color — and
    roasts accordingly ('lame and repetitive', user feedback). At
    injection, personal life now precedes trades: Personality → Voice →
    Retarded takes → Recent personal life → Recent trades. This is the
    tendency-level fix; the prompt hierarchy and the P&L-monotone guard
    are the rule and the floor. Storage format unchanged; returns input
    untouched when either section is missing or already ordered."""
    if (not text or "**Recent trades.**" not in text
            or "**Recent personal life.**" not in text):
        return text
    parts = [p for p in _PROFILE_SECTION_SPLIT_RE.split(text) if p.strip()]
    idx_tr = next((i for i, s in enumerate(parts)
                   if s.lstrip().startswith("**Recent trades.**")), None)
    idx_pl = next((i for i, s in enumerate(parts)
                   if s.lstrip().startswith("**Recent personal life.**")),
                  None)
    if idx_tr is None or idx_pl is None or idx_pl < idx_tr:
        return text
    parts[idx_tr], parts[idx_pl] = parts[idx_pl], parts[idx_tr]
    return "\n\n".join(p.strip() for p in parts)


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
    profiles = _db.get_profiles_for_users(user_ids)
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
    trader_rank_by_uid, trader_rank_total = _db.get_global_trader_ranks()

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
        text = _db._reorder_profile_for_roast_attention(
            (p.get("profile_text") or "").strip()
        )
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


def vacuum_db() -> dict:
    """VACUUM the database (weekly, quiet hour). Reclaims pages freed by
    the retention purges so the file — and the OS page cache Railway
    meters as memory — shrinks. Returns before/after page counts.
    Cannot run inside a transaction; brief write lock."""
    conn = _db.get_connection()
    before = conn.execute("PRAGMA page_count").fetchone()[0]
    free_before = conn.execute("PRAGMA freelist_count").fetchone()[0]
    conn.commit()
    conn.execute("VACUUM")
    after = conn.execute("PRAGMA page_count").fetchone()[0]
    return {"pages_before": before, "freelist_before": free_before,
            "pages_after": after}
