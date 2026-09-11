"""Post to X (Twitter) from the worker: the omni-calendar sheet nightly.

Owner ask 2026-09-11. Stdlib only: OAuth 1.0a user-context signing
(HMAC-SHA1) over urllib, no new pinned dependency. X API v2 for both
the media upload and the post; the v1.1 upload host is tried second
because X has moved that endpoint twice.

Safety:
- Nothing is sent unless X_POST_ENABLED is true AND all four keys are
  set. Disabled, `post_calendar` logs the caption it would have posted
  ("dry run") and returns None, so the pipeline can be watched for days
  before the first public post.
- One post per calendar date, recorded under /data/x-posts, so a
  redeploy cannot repost.
- Secrets never reach a log line; errors log the HTTP status and the
  first 300 chars of X's response body only.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

from config import settings

log = logging.getLogger(__name__)

X_POST_URL = "https://api.x.com/2/tweets"
X_MEDIA_URLS = ("https://api.x.com/2/media/upload",
                "https://upload.twitter.com/1.1/media/upload.json")
_UA = "omnibeta-x/1.0"


# ------------------------------------------------------------- OAuth 1.0a

def _pct(s: str) -> str:
    return urllib.parse.quote(str(s), safe="")


def oauth1_signature(method: str, url: str, params: dict, consumer_secret: str,
                     token_secret: str) -> str:
    """RFC 5849 HMAC-SHA1 signature. `params` holds the oauth_* fields
    plus any query-string or form-encoded body parameters; JSON and
    multipart bodies contribute nothing."""
    base_url = url.split("?", 1)[0]
    norm = "&".join(f"{_pct(k)}={_pct(v)}" for k, v in sorted(params.items()))
    base = f"{method.upper()}&{_pct(base_url)}&{_pct(norm)}"
    key = f"{_pct(consumer_secret)}&{_pct(token_secret)}".encode()
    return base64.b64encode(hmac.new(key, base.encode(), hashlib.sha1).digest()).decode()


def oauth1_header(method: str, url: str, *, consumer_key: str, consumer_secret: str,
                  token: str, token_secret: str, extra_params: dict | None = None,
                  nonce: str | None = None, timestamp: str | None = None) -> str:
    oauth = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": timestamp or str(int(time.time())),
        "oauth_token": token,
        "oauth_version": "1.0",
    }
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query, keep_blank_values=True))
    signed = {**oauth, **q, **(extra_params or {})}
    oauth["oauth_signature"] = oauth1_signature(method, url, signed, consumer_secret, token_secret)
    return "OAuth " + ", ".join(f'{_pct(k)}="{_pct(v)}"' for k, v in sorted(oauth.items()))


def _creds() -> dict | None:
    c = {"consumer_key": settings.x_api_key, "consumer_secret": settings.x_api_secret,
         "token": settings.x_access_token, "token_secret": settings.x_access_secret}
    return c if all(c.values()) else None


def _request(method: str, url: str, body: bytes, content_type: str, creds: dict,
             extra_params: dict | None = None) -> tuple[int, dict]:
    auth = oauth1_header(method, url, extra_params=extra_params, **creds)
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Authorization": auth, "Content-Type": content_type, "User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()[:300].decode("utf-8", "replace")
        log.warning(f"x: {method} {url.split('?')[0]} -> HTTP {e.code}: {raw}")
        return e.code, {"error": raw}


# ---------------------------------------------------------------- calls

def upload_media(png: bytes, creds: dict) -> str | None:
    """Multipart image upload; returns the media id or None. Tries the
    v2 endpoint, then the legacy host."""
    boundary = "----omnibeta" + uuid.uuid4().hex
    crlf = b"\r\n"
    body = b"--" + boundary.encode() + crlf
    body += b'Content-Disposition: form-data; name="media_category"' + crlf + crlf + b"tweet_image" + crlf
    body += b"--" + boundary.encode() + crlf
    body += b'Content-Disposition: form-data; name="media"; filename="calendar.png"' + crlf
    body += b"Content-Type: image/png" + crlf + crlf + png + crlf
    body += b"--" + boundary.encode() + b"--" + crlf
    ctype = f"multipart/form-data; boundary={boundary}"
    for url in X_MEDIA_URLS:
        status, data = _request("POST", url, body, ctype, creds)
        if 200 <= status < 300:
            mid = (data.get("data") or {}).get("id") or data.get("media_id_string") or data.get("media_id")
            if mid:
                return str(mid)
            log.warning(f"x: media upload answered {status} without an id: {str(data)[:200]}")
            return None
        if status not in (404, 410):
            return None
    return None


def create_post(text: str, media_ids: list[str], creds: dict) -> str | None:
    payload: dict = {"text": text}
    if media_ids:
        payload["media"] = {"media_ids": media_ids}
    status, data = _request("POST", X_POST_URL, json.dumps(payload).encode(), "application/json", creds)
    if 200 <= status < 300:
        return str((data.get("data") or {}).get("id") or "") or None
    return None


# --------------------------------------------------------------- ledger

def _ledger_path(date_iso: str) -> Path:
    return Path(settings.db_path).resolve().parent / "x-posts" / f"{date_iso}.json"


def already_posted(date_iso: str, key: str) -> bool:
    try:
        return key in json.loads(_ledger_path(date_iso).read_text(encoding="utf-8"))
    except Exception:
        return False


def mark_posted(date_iso: str, key: str, post_id: str, text: str) -> None:
    p = _ledger_path(date_iso)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        d = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        d = {}
    d[key] = {"post_id": post_id, "text": text,
              "at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")}
    p.write_text(json.dumps(d, indent=1), encoding="utf-8")


# ----------------------------------------------------------- the entry

def post_image(text: str, png: bytes, *, key: str, date_iso: str) -> str | None:
    """Post `text` with `png` attached, once per (date, key). Returns the
    post id, or None (disabled, dry run, duplicate, or X refused)."""
    if already_posted(date_iso, key):
        log.info(f"x: {key} for {date_iso} already posted — skipping")
        return None
    if not settings.x_post_enabled:
        log.info(f"x: DRY RUN ({key} {date_iso}, {len(png)} bytes) — would post:\n{text}")
        return None
    creds = _creds()
    if not creds:
        log.warning("x: X_POST_ENABLED is true but one or more X_* keys are missing — not posting")
        return None
    mid = upload_media(png, creds)
    if not mid:
        log.error(f"x: media upload failed for {key} {date_iso}; not posting")
        return None
    pid = create_post(text, [mid], creds)
    if pid:
        mark_posted(date_iso, key, pid, text)
        log.info(f"x: posted {key} for {date_iso}: https://x.com/i/status/{pid}")
    else:
        log.error(f"x: post failed for {key} {date_iso}")
    return pid


def post_calendar(date_iso: str, day, png: bytes) -> str | None:
    from report.calendar_caption import calendar_caption
    return post_image(calendar_caption(day), png, key="calendar", date_iso=date_iso)
