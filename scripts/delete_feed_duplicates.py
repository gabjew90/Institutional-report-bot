"""Remove the repeat posts a database lock left in the ingestion feed (2026-11-02).

The feed re-posted one note a minute while its cursor could not be saved. This
finds the bot's own posts in the feed channel since a date, groups them by title,
and for a note posted at least MIN_COPIES times keeps the earliest copy. Two posts
with one title can be two different files, so a smaller repeat is left alone, and
so is the catch-up summary.

Lists by default and deletes nothing. Deleting needs --delete and cannot be undone.
It needs the bot token, so run it with the worker's variables. It never touches
the database:

    railway run --service worker py -3.12 scripts/delete_feed_duplicates.py
    railway run --service worker py -3.12 scripts/delete_feed_duplicates.py --delete
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

API = "https://discord.com/api/v10"
DISCORD_EPOCH_MS = 1420070400000
MIN_COPIES = 5


def _request(method: str, path: str, token: str):
    """One call, waiting out rate limits. None for a 404 (already gone)."""
    for _ in range(8):
        req = urllib.request.Request(API + path, method=method, headers={
            "Authorization": "Bot " + token, "User-Agent": "DiscordBot (feed-cleanup, 1)"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                body = r.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(float(json.load(e).get("retry_after", 2)) + 0.2)
                continue
            if e.code == 404:
                return None
            raise
    raise RuntimeError(f"still rate limited: {method} {path[:50]}")


def _history(channel_id: int, after: int, token: str) -> list[dict]:
    out: list[dict] = []
    while True:
        batch = _request("GET", f"/channels/{channel_id}/messages?limit=100&after={after}", token) or []
        if not batch:
            return out
        batch.sort(key=lambda m: int(m["id"]))
        out += batch
        after = int(batch[-1]["id"])
        if len(batch) < 100:
            return out
        time.sleep(0.25)


def repeats(messages: list[dict], bot_id: str, min_copies: int = MIN_COPIES) -> dict[str, list[dict]]:
    """Title -> the copies to delete, oldest kept. Only the bot's own posts."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for m in messages:
        if m.get("author", {}).get("id") != bot_id:
            continue
        title = ((m.get("embeds") or [{}])[0].get("title") or "").strip()
        if title and "catch-up" not in title.lower():
            groups[title].append(m)
    out = {}
    for title, copies in groups.items():
        if len(copies) >= min_copies:
            copies.sort(key=lambda m: int(m["id"]))
            out[title] = copies[1:]
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--since", default="2026-11-02", help="UTC date to look back to")
    ap.add_argument("--delete", action="store_true", help="delete them; without this it only lists")
    args = ap.parse_args(argv)
    from config import settings
    token = settings.discord_bot_token
    channel_id = (settings.discord_ingest_feed_channel_id or "").strip()
    if not token or not channel_id.isdigit():
        print("Bot token or feed channel is not set. Run it with `railway run --service worker`.")
        return 2
    start = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
    after = (int(start.timestamp() * 1000) - DISCORD_EPOCH_MS) << 22
    bot_id = _request("GET", "/users/@me", token)["id"]
    found = repeats(_history(int(channel_id), after, token), bot_id)
    total = sum(len(v) for v in found.values())
    for title, extra in found.items():
        safe = title[:70].encode("ascii", "replace").decode()
        print(f"{len(extra):4d} extra copies, {extra[0]['timestamp'][11:16]} to {extra[-1]['timestamp'][11:16]} UTC | {safe}")
    print(f"{total} posts to delete, one copy of each note kept.")
    if not args.delete:
        print("Nothing deleted. Add --delete to delete them.")
        return 0
    done = 0
    for extra in found.values():
        for m in extra:
            _request("DELETE", f"/channels/{channel_id}/messages/{m['id']}", token)
            done += 1
            if done % 25 == 0:
                print(f"deleted {done} of {total}")
            time.sleep(0.4)
    print(f"Deleted {done} posts.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
