#!/usr/bin/env python3
"""One manual, PUBLIC test post to X from the worker.

    railway ssh "cd /app && /opt/venv/bin/python scripts/x_post_test.py --check"
    railway ssh "cd /app && /opt/venv/bin/python scripts/x_post_test.py --post"

--check verifies the four keys are present and signs a request to
GET /2/users/me (read-only) to prove the credentials work. --post
renders tomorrow's calendar sheet through the real pipeline and posts
it once, regardless of X_POST_ENABLED, recording it in the ledger so
the nightly job will not post the same date again. Never imports db in
a way that opens the live connection: --post reads the calendar
through build_calendar_day, which does, so run --post only from the
worker itself (this is the worker's own process model) and only when
the owner has asked for a public test.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def check() -> int:
    from config import settings
    from report import x_client as X
    names = ("x_api_key", "x_api_secret", "x_access_token", "x_access_secret")
    missing = [n.upper() for n in names if not getattr(settings, n)]
    print("keys present:", "all four" if not missing else f"missing {missing}")
    print("X_POST_ENABLED:", settings.x_post_enabled)
    if missing:
        return 1
    creds = X._creds()
    url = "https://api.x.com/2/users/me"
    req = urllib.request.Request(url, headers={
        "Authorization": X.oauth1_header("GET", url, **creds), "User-Agent": "omnibeta-x/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read()).get("data") or {}
            print(f"credentials OK: posting as @{d.get('username')} ({d.get('name')})")
            return 0
    except urllib.error.HTTPError as e:
        print(f"credentials rejected: HTTP {e.code}: {e.read()[:300].decode('utf-8', 'replace')}")
        return 1


def post() -> int:
    from datetime import datetime
    import pytz
    from config import settings
    from world_context import next_trading_day
    from report.calendar_data import build_calendar_day
    from report.calendar_render import render_calendar_png
    from report import x_client as X
    date_iso = next_trading_day(datetime.now(pytz.timezone(settings.timezone)).strftime("%Y-%m-%d"))
    day = build_calendar_day(date_iso)
    png = render_calendar_png(day)
    from report.calendar_caption import calendar_caption
    text = calendar_caption(day)
    print("caption:\n" + text)
    settings.x_post_enabled = True  # explicit owner-run test overrides the switch
    pid = X.post_image(text, png, key="calendar", date_iso=date_iso)
    print("posted:", f"https://x.com/i/status/{pid}" if pid else "FAILED (see log)")
    return 0 if pid else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--post", action="store_true")
    a = ap.parse_args()
    if a.post:
        sys.exit(post())
    sys.exit(check())
