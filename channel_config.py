"""Which Discord channel is which: by permanent ID first, name as fallback.

Until 2026-10-04 every channel setting was a channel NAME, and Discord
channels get renamed: "🚨-0dte-lotto-alerts-🚨" became Cemini's channel on
2026-08-13, and kloh's "🦉-kloh-alerts-🦉" became "🦉-big-gloh-alerts-🦉"
around 10-02, after which the bot read nothing from it until an audit
noticed on 10-04. An ID never changes. Settings now hold IDs (a name still
works, for an environment variable written the old way), and every
channel check goes through this module instead of comparing names in
place.

Owner decisions, 2026-10-04: read every channel except test-channel
(new channels included automatically); admin commands run in
test-channel.
"""
from __future__ import annotations

from typing import Iterable

# Current names, for the reader. Matching never uses this table.
KNOWN = {
    1317587853282119745: "💬-stonks-yapping-💬",
    1317606449341534222: "₿-crypto-yapping-₿",
    1317744932974235658: "🎲-gambling-yapping-🎲",
    1410655131631878174: "🏃-fitness-yapping-🏋",
    1537498564090134538: "🏈-fantasy-football-yapping-🏈",
    1317587853282119747: "🥷🏽-abe-alerts-🥷🏽",
    1420507457708621824: "💅🏾-kyle-alerts-💅🏾",
    1415429535268343919: "🦉-big-gloh-alerts-🦉",          # kloh's
    1415073939184422973: "🧙🏻‍♀️-wiz-of-cemini-alerts-🐸",
    1498438283380789248: "🫦-zhawk-thawghts-🗣",
    1408508350202908845: "🕰️-member-alerts-🕰️",
    1405199403286794393: "🐄-spot-bag-alerts-🐄",
    1407763027339509950: "🪙-crypto-alerts-🪙",
    1404879999239979251: "💲-gain-loss-porn-💲",
    1458515262168109253: "test-channel",
}
TEST_CHANNEL_ID = 1458515262168109253
# The league channel: questions asked there are league questions first
# (discord_bot/ask_router.in_fantasy_channel).
FANTASY_CHANNEL_ID = 1537498564090134538


def parse(spec) -> set[str]:
    """A comma-separated setting (or an iterable) as a set of lowercase
    entries: IDs as digit strings, names as written."""
    if spec is None:
        return set()
    items: Iterable = spec.split(",") if isinstance(spec, str) else spec
    return {str(s).strip().lower() for s in items if str(s).strip()}


def _id_and_name(channel) -> tuple[str, str]:
    """(id, lowercase name) for a discord channel object, an int ID, or a
    plain name string."""
    if isinstance(channel, int):
        return str(channel), ""
    if isinstance(channel, str):
        return "", channel.strip().lower()
    return (str(getattr(channel, "id", "") or ""),
            (getattr(channel, "name", "") or "").strip().lower())


def matches(channel, entries: set[str]) -> bool:
    """The channel is named in `entries` by its ID or, failing that, by
    its current name."""
    cid, name = _id_and_name(channel)
    return bool((cid and cid in entries) or (name and name in entries))


def ingests(channel) -> bool:
    """Store this channel's messages in chat_messages. All channels by
    default, minus the exclude list; a non-empty CHAT_INGESTION_CHANNELS
    narrows it to that list instead."""
    from config import settings
    exclude = parse(settings.chat_ingest_exclude)
    # a thread has its own ID: exclude it with its parent channel
    parent = getattr(channel, "parent_id", None)
    if matches(channel, exclude) or (parent and str(parent) in exclude):
        return False
    narrowed = parse(settings.chat_ingestion_channels)
    if narrowed:
        return matches(channel, narrowed)
    return bool(settings.chat_ingest_all_channels) or matches(
        channel, parse(settings.profile_channels) | parse(settings.chat_eager_ocr_channels))


def eager_ocr(channel) -> bool:
    """OCR this channel's screenshots at ingest, and read its text posts
    for trades (the alert rooms)."""
    from config import settings
    return matches(channel, parse(settings.chat_eager_ocr_channels))


def caller_for(channel) -> dict | None:
    """The registered analyst caller who owns this channel, by the
    caller's `channel_id`, else its `channel` name."""
    from config import settings
    cid, name = _id_and_name(channel)
    for c in settings.resolve_analyst_callers():
        if cid and str(c.get("channel_id") or "") == cid:
            return c
        if name and (c.get("channel") or "").strip().lower() == name:
            return c
    return None


def is_command_channel(channel) -> bool:
    """Admin and pulse commands may run here. An empty setting means
    anywhere."""
    from config import settings
    allowed = parse(settings.pulse_command_channels)
    return not allowed or matches(channel, allowed)


def label(entry: str) -> str:
    """'#name' for an ID entry the table knows, the entry itself
    otherwise."""
    if entry.isdigit() and int(entry) in KNOWN:
        return f"#{KNOWN[int(entry)]}"
    return entry if entry.isdigit() else f"#{entry}"
