"""Fetch live market news to fill gaps between research publication and pulse time.

Uses Finnhub's free general market news endpoint. If FINNHUB_API_KEY is not set,
returns an empty snapshot and the synthesizer falls back to research-only context.

Finnhub free tier: 60 calls/min, generous for our once-a-day pulse use.
Sign up at https://finnhub.io/register for a free key.
"""

import json
import logging
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from config import settings

log = logging.getLogger(__name__)


def _fetch_json(url: str, timeout: float = 8.0) -> list | dict | None:
    req = urllib.request.Request(url, headers={"User-Agent": "MarketPulseBot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log.warning(f"News fetch failed: {e}")
        return None


def fetch_news_snapshot(since_hours: int = 48, limit: int = 15) -> str:
    """Return a human-readable digest of recent market news for the prompt.

    Returns a string; if no key is set or fetch fails, returns a placeholder.
    """
    key = settings.finnhub_api_key
    if not key:
        return "LIVE NEWS: (no FINNHUB_API_KEY set — research-only mode)"

    # General market news
    url = f"https://finnhub.io/api/v1/news?category=general&token={urllib.parse.quote(key)}"
    data = _fetch_json(url)
    if not data or not isinstance(data, list):
        return "LIVE NEWS: (fetch failed, check key / network)"

    cutoff_ts = (datetime.utcnow() - timedelta(hours=since_hours)).timestamp()
    fresh = [n for n in data if n.get("datetime", 0) >= cutoff_ts]
    # Sort newest first
    fresh.sort(key=lambda n: n.get("datetime", 0), reverse=True)
    fresh = fresh[:limit]

    if not fresh:
        return f"LIVE NEWS (last {since_hours}h): no fresh stories from Finnhub."

    lines = [f"LIVE MARKET NEWS (last {since_hours}h from Finnhub, newest first):"]
    for n in fresh:
        ts = datetime.utcfromtimestamp(n.get("datetime", 0)).strftime("%Y-%m-%d %H:%M UTC")
        headline = n.get("headline", "").strip()
        source = n.get("source", "").strip()
        summary = (n.get("summary") or "").strip()[:200]
        lines.append(f"  [{ts}] {source}: {headline}")
        if summary:
            lines.append(f"    {summary}")
    return "\n".join(lines)
