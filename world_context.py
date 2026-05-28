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

# ─── Owned alert-channel mapping (profile-builder Path A unlock) ────
# Explicit username → owned-channel mapping so Path A detection in the
# profile builder is DETERMINISTIC, not pattern-match. Was missing
# previously: the model correctly matched `abe-alerts` to abullish_xyz
# but missed `kyle-alerts` → bankerkyle because of the alias gap.
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
    instead of relying on substring pattern-match. The Path A unlock
    rule references this block by sentinel."""
    lines = [
        "KNOWN OWNED ALERT CHANNELS (deterministic Path A mapping — do not",
        "infer ownership from channel-name pattern; use this table):",
    ]
    for ch, user, disp in OWNED_ALERT_CHANNELS:
        if user == "*shared":
            lines.append(f"  - `{ch}` — shared P&L screenshots (no single owner)")
        else:
            lines.append(f"  - `{ch}` — owned by `{user}` (display: {disp})")
    lines.append(
        "If you are profiling one of the named users above AND they have"
    )
    lines.append(
        "a meaningful stream of posts in their owned channel (≥10 posts"
    )
    lines.append(
        "in the window), Path A unlocks unconditionally — they get the"
    )
    lines.append(
        "75+ window. Position inside is set by trade quality alone."
    )
    lines.append(
        "Channels NOT in this table are shared alert channels (e.g."
    )
    lines.append(
        "member-alerts, spot-bag-alerts, 0dte-lotto-alerts, crypto-alerts);"
    )
    lines.append(
        "sustained posting in those CAN unlock Path A but requires a real"
    )
    lines.append(
        "stream (≥15 entry alerts in the window), not occasional mentions."
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
