"""/ask scoreboard (2026-10-28): what the answer pipeline does, per question
shape and per after-the-fact check, read from the ask logs.

The point is to decide where to fix things. A check that fires on a large
share of one shape means the input to that shape is wrong (route, prefetch,
outline), and the fix belongs there. A check that never fires is a deletion
candidate. The catch-all shape ("unknown") has no prefetch and no tool
restriction, so its share and its questions say which shape to add next.

Reads the markdown logs `db.append_ask_interaction` writes, one file per
UTC day. Nothing is written and `db` is never imported, so it runs on the
worker against /data/ask-logs or locally against the copy on the
pulse-data branch:

    git fetch origin pulse-data
    mkdir -p /tmp/asklogs && for f in $(git ls-tree -r --name-only origin/pulse-data ask-logs); do
        git show "origin/pulse-data:$f" > /tmp/asklogs/$(basename $f); done
    py -3.12 scripts/ask_scoreboard.py --dir /tmp/asklogs --days 14

Shape: entries logged since 2026-10-28 carry the router's shape on their
Route line. Older entries get it by re-running `ask_router.classify` on the
logged question, which misses the inputs the bot adds at run time (the
asker's recent messages for a subjectless question, whether a member is
named, a reply's original asker). Those rows are counted as "inferred".

"Words changed" means the words of the model's raw output are not the start
of what was posted: a check rewrote, stripped or replaced text. A footer
under an unchanged answer does not count, and neither does the dash and
semicolon cleanup every answer gets, which is counted on its own line as
punctuation only. It is measured per entry, so a check's share says how
often the words changed in a turn where that check fired, not that this
check made the change.

The catch-all is split by the classifier's FACT/BANTER call. A banter reply
on the catch-all is expected; a factual question there is a routing gap.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from discord_bot import ask_router  # noqa: E402  (pure regex, no db)

DEFAULT_DIR = "/data/ask-logs"
UNKNOWN = ask_router.UNKNOWN

_ENTRY_RE = re.compile(r"^## (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC\s*$", re.M)
_ROUTE_RE = re.compile(r"^\*\*Route:\*\*\s*(.*)$", re.M)
_TYPE_RE = re.compile(r"^\*\*Type:\*\*\s*`([^`]+)`", re.M)
_CHANNEL_RE = re.compile(r"^\*\*Asker:\*\*.* in #(.+)$", re.M)
_Q_RE = re.compile(r"^\*\*Q:\*\*\s*(.*?)(?=^\*\*A:\*\*)", re.S | re.M)
_A_RE = re.compile(r"^\*\*A:\*\*\s*(.*?)(?=^\*\*Tools called:\*\*|^<details>|\Z)",
                   re.S | re.M)
_RAW_RE = re.compile(r"Raw model output[^\n]*\n+```text\n(.*?)\n```", re.S)
_TOOL_ROW_RE = re.compile(r"^\| ([a-z_]+) \|", re.M)


def _base_check(name: str) -> str:
    """'name-check:Wth' -> 'name-check', 'figure-provenance:stripped:1' ->
    'figure-provenance:stripped'. Trailing parts that carry a count or the
    word that triggered the check are dropped so one check counts once."""
    parts = name.strip().split(":")
    while len(parts) > 1 and (parts[-1].isdigit() or parts[-1][:1].isupper()):
        parts.pop()
    return ":".join(parts)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _words(s: str) -> str:
    return re.sub(r"[\W_]+", " ", (s or "").lower()).strip()


def parse_entry(ts: str, block: str) -> dict:
    head = block.split("<details>", 1)[0]
    route_m = _ROUTE_RE.search(head)
    route_line = route_m.group(1) if route_m else ""
    type_m = _TYPE_RE.search(head)
    chan_m = _CHANNEL_RE.search(head)
    q_m = _Q_RE.search(head)
    a_m = _A_RE.search(block)
    raw_m = _RAW_RE.search(block)

    rk = re.search(r"`([A-Z]+)/([A-Z?]+)`", route_line)
    lat = re.search(r"(\d+(?:\.\d+)?)s\b", route_line)
    rc = re.search(r"retry-calls: (\d+)", route_line)
    shape_m = re.search(r"shape: ([a-z_]+)", route_line)
    g_m = re.search(r"guards: (.*)$", route_line)
    guards = []
    if g_m and g_m.group(1).strip() not in ("", "—"):
        guards = [g.strip() for g in g_m.group(1).split(",") if g.strip()]

    answer = (a_m.group(1) if a_m else "").strip()
    raw = (raw_m.group(1) if raw_m else "").strip()
    differs = bool(raw) and not _norm(answer).startswith(_norm(raw))
    changed = differs and not _words(answer).startswith(_words(raw))

    _, _, tools_part = block.partition("**Tools called:**")
    tools = _TOOL_ROW_RE.findall(tools_part.split("<details>", 1)[0])

    return {
        "ts": ts,
        "channel": chan_m.group(1).strip() if chan_m else "",
        "type": type_m.group(1) if type_m else "gemini",
        "route": rk.group(1) if rk else "",
        "kind": rk.group(2) if rk else "",
        "grounded": "grounded ✅" in route_line,
        "latency_s": float(lat.group(1)) if lat else None,
        "retry_calls": int(rc.group(1)) if rc else 0,
        "retried": "retry:" in route_line or bool(rc),
        "shape": shape_m.group(1) if shape_m else "",
        "shape_inferred": False,
        "checks": guards,
        "tools": [t for t in tools if t != "tool"],
        "question": (q_m.group(1) if q_m else "").strip(),
        "answer_changed": changed,
        "punctuation_only": differs and not changed,
        "has_route_line": bool(route_line),
    }


def infer_shape(entry: dict) -> None:
    if entry["shape"]:
        return
    try:
        r = ask_router.classify(entry["question"], fantasy_enabled=True,
                                channel_name=entry["channel"])
        entry["shape"] = r.shape
    except Exception:
        entry["shape"] = UNKNOWN
    entry["shape_inferred"] = True


def group(entry: dict) -> str:
    """The table row: the shape, with the catch-all split by FACT/BANTER."""
    if entry["shape"] == UNKNOWN:
        return f"{UNKNOWN}/{(entry['kind'] or '?').lower()}"
    return entry["shape"]


def load(log_dir: str, since: datetime) -> list[dict]:
    out = []
    for path in sorted(glob.glob(os.path.join(log_dir, "*.md"))):
        day = os.path.basename(path)[:10]
        try:
            if datetime.fromisoformat(day + "T23:59:59").replace(tzinfo=timezone.utc) < since:
                continue
        except ValueError:
            continue
        text = open(path, encoding="utf-8", errors="replace").read()
        marks = list(_ENTRY_RE.finditer(text))
        for i, m in enumerate(marks):
            ts = datetime.fromisoformat(m.group(1)).replace(tzinfo=timezone.utc)
            if ts < since:
                continue
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            e = parse_entry(m.group(1), text[m.end():end])
            infer_shape(e)
            out.append(e)
    return out


def _pct(n: int, d: int) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


def _p90(xs: list[float]) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(0.9 * (len(xs) - 1))))]


def summarize(entries: list[dict], examples: int = 15) -> dict:
    n = len(entries)
    by_shape: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        by_shape[group(e)].append(e)

    shapes = []
    for shape, es in sorted(by_shape.items(), key=lambda kv: -len(kv[1])):
        lats = [e["latency_s"] for e in es if e["latency_s"] is not None]
        checks = Counter(_base_check(c) for e in es for c in e["checks"])
        tools = Counter(t for e in es for t in e["tools"])
        shapes.append({
            "shape": shape,
            "asks": len(es),
            "share_pct": _pct(len(es), n),
            "inferred": sum(e["shape_inferred"] for e in es),
            "median_s": round(statistics.median(lats), 1) if lats else None,
            "p90_s": _p90(lats),
            "changed_pct": _pct(sum(e["answer_changed"] for e in es), len(es)),
            "retried_pct": _pct(sum(e["retried"] for e in es), len(es)),
            "failed": sum(e["type"] == "failed" for e in es),
            "top_checks": checks.most_common(5),
            "top_tools": tools.most_common(5),
        })

    fires: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        for c in {_base_check(c) for c in e["checks"]}:
            fires[c].append(e)
    checks = []
    for c, es in sorted(fires.items(), key=lambda kv: -len(kv[1])):
        checks.append({
            "check": c,
            "fires": len(es),
            "share_pct": _pct(len(es), n),
            "changed_pct": _pct(sum(e["answer_changed"] for e in es), len(es)),
            "shapes": Counter(group(e) for e in es).most_common(3),
        })

    unknown = [e for e in entries if e["shape"] == UNKNOWN and e["kind"] != "BANTER"]
    return {
        "asks": n,
        "first": entries[0]["ts"] if entries else None,
        "last": entries[-1]["ts"] if entries else None,
        "failed": sum(e["type"] == "failed" for e in entries),
        "short_circuit": sum(e["type"].startswith("short_circuit") for e in entries),
        "changed": sum(e["answer_changed"] for e in entries),
        "punctuation_only": sum(e["punctuation_only"] for e in entries),
        "no_check": sum(not e["checks"] for e in entries),
        "inferred": sum(e["shape_inferred"] for e in entries),
        "shapes": shapes,
        "checks": checks,
        "unknown_examples": [
            {"ts": e["ts"], "channel": e["channel"],
             "question": ask_router._last_line(e["question"])[:140]}
            for e in unknown[-examples:]],
    }


def _fmt_pairs(pairs) -> str:
    return ", ".join(f"{k} {v}" for k, v in pairs) or "—"


def render(s: dict) -> str:
    n = s["asks"]
    lines = [
        f"# /ask scoreboard: {s['first']} to {s['last']} UTC",
        "",
        f"- Answers: {n} (failed {s['failed']}, short-circuit {s['short_circuit']})",
        f"- Words changed after the model wrote them: {s['changed']} ({_pct(s['changed'], n)}%)",
        f"- Punctuation cleanup only: {s['punctuation_only']} ({_pct(s['punctuation_only'], n)}%)",
        f"- No check fired: {s['no_check']} ({_pct(s['no_check'], n)}%)",
        f"- Shape inferred by re-running the router: {s['inferred']} of {n}",
        "",
        "## By shape",
        "",
        "| shape | asks | share | median s | p90 s | words changed | retried | failed | top checks | top tools |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in s["shapes"]:
        lines.append(
            f"| {r['shape']} | {r['asks']} | {r['share_pct']}% | {r['median_s']} | "
            f"{r['p90_s']} | {r['changed_pct']}% | {r['retried_pct']}% | {r['failed']} | "
            f"{_fmt_pairs(r['top_checks'])} | {_fmt_pairs(r['top_tools'])} |")
    lines += [
        "",
        "## By check",
        "",
        "| check | fires | share of answers | words changed in those turns | shapes |",
        "|---|---|---|---|---|",
    ]
    for r in s["checks"]:
        lines.append(f"| {r['check']} | {r['fires']} | {r['share_pct']}% | "
                     f"{r['changed_pct']}% | {_fmt_pairs(r['shapes'])} |")
    if s["unknown_examples"]:
        lines += ["", "## Latest factual questions on the catch-all", ""]
        lines += [f"- {e['ts'][:16]} #{e['channel']}: {e['question']}"
                  for e in s["unknown_examples"]]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dir", default=DEFAULT_DIR)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--since", help="UTC time, overrides --days")
    ap.add_argument("--examples", type=int, default=15)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    since = (datetime.fromisoformat(a.since.replace("Z", "")).replace(tzinfo=timezone.utc)
             if a.since else datetime.now(timezone.utc) - timedelta(days=a.days))
    s = summarize(load(a.dir, since), a.examples)
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(s, indent=1, ensure_ascii=False) if a.json else render(s))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
