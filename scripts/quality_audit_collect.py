"""Evidence bundle for the quality audit (.claude/skills/quality-audit, 2026-10-02).

Runs ON THE WORKER, read-only, and prints one JSON document with what the
audit needs since a given UTC time:

  asks       every /ask entry in /data/ask-logs: asker, channel, route,
             guards, question, answer (with its Sources/Data footer and
             tool table), the WHO'S TALKING header lines the model was
             shown, its own earlier answers to that asker ("YOU said
             earlier"), the asker's real trades for the last 21 days, and
             the channel's next 15 minutes of chat (the room's reaction)
  profiles   user_profiles rows updated since then, full text
  prints     economic print alerts posted since then (/data/print-alerts),
             with the exact posted lines and the post time
  calendar   omni-calendar posts since then (calendar_posts), the matching
             X post record (/data/x-posts), and the posted image and lineup the
             calendar job kept at post time
  ingestion  research PDFs per day, so a stalled feed shows up

Nothing is written. The database is
opened with mode=ro and `db` is never imported (CLAUDE.md: a second
process must not take the schema lock).

    python scripts/quality_audit_collect.py --since 2026-10-01T00:00:00Z
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

BOT_ID = 1422761344322502807
DB = "file:/data/reports.db?mode=ro"
ASK_LOGS = "/data/ask-logs"
PRINTS = "/data/print-alerts"
X_POSTS = "/data/x-posts"
CAL_PNG = "/data/calendar-posts"


def _utc(s: str) -> datetime:
    s = s.strip().replace(" UTC", "").replace("Z", "").replace(" ", "T")
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _sp(dt: datetime) -> str:
    """SQLite datetime('now') format, space separator. Columns written by
    SQL defaults (updated_at, posted_at, created_at) use it, and a
    T-format bound compares wrong on the same day (CLAUDE.md, timestamp
    normalization)."""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _db():
    return sqlite3.connect(DB, uri=True, timeout=5)


# ------------------------------------------------------------------ asks

_ENTRY_RE = re.compile(r"^## (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC\s*$", re.M)


def _field(block: str, name: str) -> str:
    m = re.search(rf"^\*\*{name}:\*\*\s*(.*)$", block, re.M)
    return m.group(1).strip() if m else ""


def _parse_entry(ts: str, block: str) -> dict:
    head, _, details = block.partition("<details>")
    q_m = re.search(r"^\*\*Q:\*\*\s*(.*?)(?=^\*\*A:\*\*)", head, re.S | re.M)
    a_m = re.search(r"^\*\*A:\*\*\s*(.*)$", head, re.S | re.M)
    asker = _field(head, "Asker")
    uname = re.search(r"\(`([^`]+)`\)", asker)
    chan = re.search(r" in #(.+)$", asker)
    # What the model was shown about people, and its own earlier answers.
    whos = [ln.strip() for ln in details.splitlines()
            if re.match(r"\s*- \*\*[^*]+\*\* \(", ln)]
    earlier = [ln.strip()[:400] for ln in details.splitlines()
               if ln.strip().startswith("[YOU said earlier")]
    return {
        "ts": ts,
        "asker": asker,
        "asker_username": uname.group(1) if uname else "",
        "channel": chan.group(1).strip() if chan else "",
        "route": _field(head, "Route"),
        "question": (q_m.group(1).strip() if q_m else "")[:3000],
        "answer": (a_m.group(1).strip() if a_m else "")[:12000],
        "whos_talking": [w[:600] for w in whos][:12],
        "you_said_earlier": earlier[:8],
    }


def collect_asks(since: datetime) -> list[dict]:
    out = []
    for path in sorted(glob.glob(f"{ASK_LOGS}/*.md")):
        day = os.path.basename(path)[:10]
        try:
            if _utc(day + "T23:59:59") < since:
                continue
        except ValueError:
            continue
        text = open(path, encoding="utf-8", errors="replace").read()
        marks = list(_ENTRY_RE.finditer(text))
        for i, m in enumerate(marks):
            if _utc(m.group(1)) < since:
                continue
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            out.append(_parse_entry(m.group(1), text[m.end():end]))
    return out


def enrich_asks(asks: list[dict]) -> None:
    c = _db()
    for a in asks:
        t0 = _utc(a["ts"])
        # The asker's documented trades: the truth behind any P&L joke.
        row = c.execute("select user_id from user_profiles where username=?",
                        (a["asker_username"],)).fetchone()
        a["asker_trades_21d"] = []
        if row:
            for r in c.execute(
                    "select posted_at,ticker,contract_type,strike,expiry,action,gain_pct,"
                    "inferred_status from analyst_trades where author_id=? and is_trade=1 "
                    "and posted_at>=? order by posted_at desc limit 25",
                    (row[0], _iso(t0 - timedelta(days=21)))):
                a["asker_trades_21d"].append(dict(zip(
                    ("posted_at", "ticker", "type", "strike", "expiry", "action",
                     "gain_pct", "status"), r)))
        # The room's next 15 minutes in that channel.
        a["room_after"] = [
            f"{r[0][11:16]} {r[1]}: {(r[2] or '')[:300]}"
            for r in c.execute(
                "select posted_at,coalesce(author_display,author_username),content "
                "from chat_messages where channel_name=? and posted_at>? and posted_at<=? "
                "and author_id!=? order by posted_at limit 15",
                (a["channel"], _iso(t0), _iso(t0 + timedelta(minutes=15)), BOT_ID))
        ]


# -------------------------------------------------------------- profiles

def collect_profiles(since: datetime) -> list[dict]:
    c = _db()
    rows = c.execute(
        "select user_id,username,display_name,updated_at,profile_text,personal_ammo,"
        "trader_rank,trader_rationale,racism_rationale from user_profiles "
        "where updated_at>=? order by updated_at", (_sp(since),)).fetchall()
    keys = ("user_id", "username", "display_name", "updated_at", "profile_text",
            "personal_ammo", "trader_rank", "trader_rationale", "racism_rationale")
    return [dict(zip(keys, r)) for r in rows]


# ---------------------------------------------------------------- prints

def collect_prints(since: datetime) -> list[dict]:
    out = []
    for path in sorted(glob.glob(f"{PRINTS}/*.json")):
        try:
            data = json.load(open(path, encoding="utf-8"))
        except Exception as e:
            out.append({"file": path, "error": str(e)})
            continue
        for kind, rec in (data or {}).items():
            at = (rec or {}).get("at")
            if at and _utc(at) >= since:
                out.append({"date": os.path.basename(path)[:10], "kind": kind,
                            "posted_at_utc": at, "lines": rec.get("lines") or [],
                            "rows": rec.get("rows") or []})
    return out


# -------------------------------------------------------------- calendar

def collect_calendar(since: datetime) -> list[dict]:
    """The posted sheet as the job kept it: the PNG under
    /data/calendar-posts/ and the lineup JSON in calendar_posts (both
    written at post time since 2026-10-02; the bot cannot read its own
    message back from the channel, Discord answers 403)."""
    c = _db()
    out = []
    for date_iso, ch, posted, refreshed, lineup in c.execute(
            "select date_iso,channel_id,posted_at,refreshed_at,lineup_json from calendar_posts "
            "where posted_at>=? order by posted_at", (_sp(since),)):
        png = f"{CAL_PNG}/{date_iso}.png"
        out.append({"date": date_iso, "channel_id": ch, "posted_at_utc": posted,
                    "refreshed_at": refreshed,
                    "image": png if os.path.exists(png) else None,
                    "lineup": json.loads(lineup) if lineup else None})
    # X posts are filed under the day the post was made.
    for path in sorted(glob.glob(f"{X_POSTS}/*.json")):
        try:
            data = json.load(open(path, encoding="utf-8"))
        except Exception:
            continue
        x = (data or {}).get("calendar") or {}
        if x.get("at") and _utc(x["at"]) >= since:
            out.append({"x_post_file": os.path.basename(path), "x_post_id": x.get("post_id"),
                        "x_posted_at_utc": x.get("at"), "x_text": x.get("text")})
    return out


# ------------------------------------------------------------- ingestion

def collect_ingestion(since: datetime) -> list:
    c = _db()
    return c.execute(
        "select substr(created_at,1,10),count(*) from pdf_files where created_at>=? "
        "group by 1 order by 1", (_sp(since - timedelta(days=1)),)).fetchall()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True, help="UTC time, e.g. 2026-10-01T00:00:00Z")
    ap.add_argument("--only", default="asks,profiles,prints,calendar,ingestion")
    a = ap.parse_args()
    since = _utc(a.since)
    want = set(a.only.split(","))
    bundle: dict = {"since_utc": _iso(since) + "Z",
                    "collected_utc": _iso(datetime.now(timezone.utc)) + "Z"}
    if "asks" in want:
        asks = collect_asks(since)
        enrich_asks(asks)
        bundle["asks"] = asks
    if "profiles" in want:
        bundle["profiles"] = collect_profiles(since)
    if "prints" in want:
        bundle["prints"] = collect_prints(since)
    if "calendar" in want:
        bundle["calendar"] = collect_calendar(since)
    if "ingestion" in want:
        bundle["ingestion_pdfs_per_day"] = collect_ingestion(since)
    json.dump(bundle, sys.stdout, ensure_ascii=False, indent=1, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
