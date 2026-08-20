"""Single source of truth for temporal world-context facts that rot.

When the Fed chair changes, when the geopolitical backdrop shifts, when
MAG7 earnings windows move — this is the ONE file to update. Every
prompt and filter that references these facts imports from here, so a
single edit propagates.

History note: the previous design hardcoded "Kevin Warsh (took over
from Powell in mid-2026)" directly into multiple prompts. When Warsh's
tenure ends, finding every reference is a grep-and-pray operation
across `ai_analysis/prompts.py`, `report/news_data.py`, the system
prompt header comments, etc. This module exists to make that update
one edit instead of N edits.

How to use:
  from world_context import (
      FED_CHAIR_PROMPT_BLOCK,
      FED_SPEAKER_KEYWORDS,
      GEOPOLITICAL_CONTEXT_BLOCK,
  )
  # Then concatenate into your prompt string at module load.

How to update when the world changes:
  - Edit the constants below.
  - Restart the bot (Railway redeploy on next push).
  - That's it.
"""

# ─── US market holidays (NYSE full closures) ────────────────────────
# The scheduled pulse SKIPS these days (2026-07-02 owner call: fire on
# market-open days only). Half-days (early closes) are NOT listed — the
# market is open, the pulse fires. UPDATE ANNUALLY: add next year's
# list before Jan 1 or the calendar goes dark for that year and
# is_us_market_holiday() returns None (callers treat None as "calendar
# doesn't cover this date — proceed normally, don't skip").
#
# Source convention: official NYSE holiday schedule, observed dates
# (a Saturday holiday observes Friday; a Sunday holiday observes Monday).

US_MARKET_HOLIDAYS = {
    # 2026
    "2026-01-01": "New Year's Day",
    "2026-01-19": "Martin Luther King Jr. Day",
    "2026-02-16": "Presidents' Day",
    "2026-04-03": "Good Friday",
    "2026-05-25": "Memorial Day",
    "2026-06-19": "Juneteenth",
    "2026-07-03": "Independence Day (observed)",
    "2026-09-07": "Labor Day",
    "2026-11-26": "Thanksgiving Day",
    "2026-12-25": "Christmas Day",
    # 2027
    "2027-01-01": "New Year's Day",
    "2027-01-18": "Martin Luther King Jr. Day",
    "2027-02-15": "Presidents' Day",
    "2027-03-26": "Good Friday",
    "2027-05-31": "Memorial Day",
    "2027-06-18": "Juneteenth (observed)",
    "2027-07-05": "Independence Day (observed)",
    "2027-09-06": "Labor Day",
    "2027-11-25": "Thanksgiving Day",
    "2027-12-24": "Christmas Day (observed)",
}

_HOLIDAY_YEARS_COVERED = {y for y in
                          {d[:4] for d in US_MARKET_HOLIDAYS}}


def is_us_market_holiday(date_iso: str) -> str | bool | None:
    """Holiday name if `date_iso` (YYYY-MM-DD) is a full NYSE closure,
    False if it's a covered year but not a holiday, None if the calendar
    doesn't cover that year (stale list — callers must proceed normally
    and NOT skip)."""
    if not date_iso or len(date_iso) < 10:
        return None
    d = date_iso[:10]
    if d[:4] not in _HOLIDAY_YEARS_COVERED:
        return None
    return US_MARKET_HOLIDAYS.get(d, False)


# ─── Fed leadership ──────────────────────────────────────────────────
# Update both when a new chair is confirmed: FED_CHAIR is the current
# chair's surname (used in news filter keywords), PREDECESSOR_NAME is
# the previous chair who may still be a sitting governor and whose
# comments retain some market relevance, _DESCRIPTION is the prose
# block that gets injected into prompts.

FED_CHAIR = "Warsh"
FED_CHAIR_FULL = "Kevin Warsh"
PREDECESSOR_NAME = "Powell"
PREDECESSOR_ROLE_NOW = "Board governor"
TRANSITION_DESCRIPTION = "took over from Powell in mid-2026"

# Pre-built prose block — drop into prompts via string concat.
FED_CHAIR_PROMPT_BLOCK = (
    f"The current Fed chair is **{FED_CHAIR_FULL}** ({TRANSITION_DESCRIPTION}); "
    f"their testimony / FOMC pressers move markets the most. "
    f"{PREDECESSOR_NAME} remains as a {PREDECESSOR_ROLE_NOW} so their "
    f"comments still pass the filter but no longer carry chair-level weight."
)

# Lowercase keyword list for the Finnhub event-name filter (substring
# match). Always include the current chair's surname and the predecessor.
FED_SPEAKER_KEYWORDS: list[str] = [
    "fomc",
    "fed chair",
    FED_CHAIR.lower(),
    PREDECESSOR_NAME.lower(),
]


# ─── Geopolitical backdrop ───────────────────────────────────────────
# Short prose describing the macro / geopolitical regime the bot is
# operating in. Used to give synthesis enough context that "Middle
# East tensions" in a PDF doesn't get over-emphasized or under-emphasized
# relative to its actual real-world salience.
#
# Keep this under ~3 sentences. The bot reads research; this block
# just frames the current backdrop. When the regime shifts (Iran
# resolution, new conflict, regime change, major sanctions package),
# edit here.

GEOPOLITICAL_CONTEXT_BLOCK = (
    "Current geopolitical backdrop (update this block when the regime "
    "shifts): Middle East tensions remain elevated with Iran-related "
    "shipping disruptions periodically affecting oil markets, "
    "ceasefire negotiations active but unstable. China trade-relations "
    "context still evolving. Treat any specific event mention in research "
    "as the source of truth for what's actually current; this block "
    "is calibration, not a content source."
)


# ─── MAG7 earnings awareness (advisory note for the prompt) ─────────
# The MAG7 hard-HIGH rule for earnings windows used to read "always
# HIGH on or near print day" with no calendar awareness — every MAG7
# mention got over-classified. The runtime cron supplies actual
# Finnhub earnings calendar data into synthesis context; the prompt
# block below sets expectations on how to use that data rather than
# hardcoding a calendar.

# ─── Owned alert-channel mapping (used by the receipts-ceiling layer) ────
# Explicit username → owned-channel mapping so the profile builder
# attributes alert-channel posts to the right user deterministically
# rather than pattern-matching channel names against usernames
# (which failed silently for `kyle-alerts` → bankerkyle because of the
# alias gap, capping BK at the wrong score).
#
# When adding a new caller, add the row here + add the channel to
# config.py's chat_eager_ocr_channels + (optionally) config.py's
# analyst_callers if you want full caller-mode tracking. This file is
# the single source of truth for "who owns which channel."
#
# Format: list of (channel_name, username, display_name) tuples.

OWNED_ALERT_CHANNELS: list[tuple[str, str, str]] = [
    # (channel_name,                    username,         display)
    ("💲-gain-loss-porn-💲",           "*shared",         "(shared P&L receipts — anyone)"),
    ("💅🏾-kyle-alerts-💅🏾",          "bankerkyle",     "BK"),
    ("🥷🏽-abe-alerts-🥷🏽",            "abullish_xyz",   "abe"),
    ("🦉-kloh-alerts-🦉",              "kloh.",           "kloh"),
    ("🫦-zhawk-thawghts-🗣",           ".zhawk",          "ZHawk"),
]

def owned_channels_prompt_block() -> str:
    """Pre-built prose block for the profile prompt. Lists each owned
    channel + its owner so the model has a deterministic mapping
    instead of relying on substring pattern-match. Used by the new
    chatter-base × receipts-ceiling rubric to identify which posts
    belong to which user when scoring the 7-day points ledger."""
    lines = [
        "KNOWN OWNED ALERT CHANNELS (deterministic owner mapping — do not",
        "infer ownership from channel-name pattern; use this table):",
    ]
    for ch, user, disp in OWNED_ALERT_CHANNELS:
        if user == "*shared":
            lines.append(f"  - `{ch}` — shared P&L screenshots (no single owner)")
        else:
            lines.append(f"  - `{ch}` — owned by `{user}` (display: {disp})")
    lines.append(
        "Posts in a user's own channel from the table above are their alert"
    )
    lines.append(
        "stream — the entries and closes appearing in the 7-DAY POINTS LEDGER"
    )
    lines.append(
        "below come from analyst_trades extracted off those messages. Channels"
    )
    lines.append(
        "NOT in this table are shared alert rooms (member-alerts,"
    )
    lines.append(
        "spot-bag-alerts, 0dte-lotto-alerts, crypto-alerts); posts there also"
    )
    lines.append(
        "feed the points ledger via member-mode extraction."
    )
    return "\n".join(lines)


MAG7_EARNINGS_AWARENESS_BLOCK = (
    "MAG7 earnings windows (binding rule): the Finnhub earnings "
    "calendar reaches synthesis pre-filtered to MAG7 + top banks + "
    "bellwethers only. A MAG7 mention in a research PDF should be "
    "weighted as HIGH-priority only when (a) the PDF is dated within "
    "~5 trading days of one of the MAG7 names' scheduled earnings, "
    "or (b) the PDF makes a directional call on the upcoming print. "
    "A passing MAG7 mention outside the print window is normal sector "
    "commentary and should be triaged on its own merits, not floored "
    "to HIGH by the mention alone."
)


# ---------------------------------------------------------------------
# Major scheduled market events with VERIFIED dates (2026-08-20).
#
# Why: Jackson Hole is not a calendar-feed row (the Tier-1 whitelist
# passes FOMC/Powell/CPI-class releases; a symposium has no "release"),
# so DRAFT dated it from desk prose — and the 8/17, 8/18 and 8/19
# pulses all placed it under "This Week" a week early. This dict is the
# ground truth the economic-calendar block injects so synthesis never
# dates these events from research vibes.
#
# Update annually alongside US_MARKET_HOLIDAYS. Source: the event's
# own organizer (Kansas City Fed for Jackson Hole).
MAJOR_MARKET_EVENTS = [
    {
        "start": "2026-08-27", "end": "2026-08-29",
        "name": "Jackson Hole Economic Policy Symposium",
        "detail": "Fed Chair Warsh keynote Friday Aug 28 morning",
    },
]


def upcoming_market_events(today_iso: str, days_ahead: int = 12) -> list[str]:
    """Human-readable lines for MAJOR_MARKET_EVENTS within the window
    (including events currently in progress). Empty list when none."""
    import datetime as _dt
    try:
        today = _dt.date.fromisoformat(str(today_iso)[:10])
    except ValueError:
        return []
    horizon = today + _dt.timedelta(days=days_ahead)
    out = []
    for ev in MAJOR_MARKET_EVENTS:
        try:
            start = _dt.date.fromisoformat(ev["start"])
            end = _dt.date.fromisoformat(ev["end"])
        except (KeyError, ValueError):
            continue
        if end < today or start > horizon:
            continue
        # %-d is not portable; compose day numbers manually
        label = (f"{start.strftime('%a %b')} {start.day}"
                 f" - {end.strftime('%a %b')} {end.day}")
        line = f"{ev['name']}: {label}"
        if ev.get("detail"):
            line += f" ({ev['detail']})"
        if start <= today <= end:
            line += " [IN PROGRESS]"
        out.append(line)
    return out
