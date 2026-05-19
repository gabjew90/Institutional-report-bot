"""One-shot script: dump recent HIGH/MEDIUM ingestions as a markdown report.

Run on the worker via: railway ssh < dump_ingestions.py
"""
import sqlite3
import json
import datetime
from collections import defaultdict

c = sqlite3.connect("/data/reports.db")
c.row_factory = sqlite3.Row

LATEST = (
    "WITH latest AS (SELECT pdf_file_id, MAX(id) AS aid "
    "FROM pdf_analyses GROUP BY pdf_file_id) "
)

rows = c.execute(LATEST + """
    SELECT pf.dropbox_path, pf.dropbox_modified_at, pa.priority, pa.analysis_json
    FROM pdf_files pf
    JOIN latest l ON l.pdf_file_id = pf.id
    JOIN pdf_analyses pa ON pa.id = l.aid
    WHERE pf.dropbox_modified_at >= datetime('now', '-24 hours')
      AND pa.priority IN ('high', 'medium')
    ORDER BY pa.priority, pf.dropbox_modified_at DESC
""").fetchall()

buckets = defaultdict(lambda: defaultdict(list))
for r in rows:
    try:
        a = json.loads(r["analysis_json"])
    except Exception:
        a = {}
    src = (a.get("source") or "Unknown").strip()
    title = (a.get("title") or "").strip()[:120] or "(no title)"
    insights = a.get("key_insights") or []
    desc = (insights[0] if insights else "").strip().replace("\n", " ")[:280]
    buckets[r["priority"]][src].append({
        "ts": (r["dropbox_modified_at"] or "")[:16],
        "path": r["dropbox_path"],
        "title": title,
        "desc": desc,
    })

print("# Recent ingestions (last 24h)")
print()
print(f"Generated: {datetime.datetime.utcnow().isoformat()[:19]} UTC  ")
print(f"Total HIGH+MEDIUM analyzed: **{len(rows)}**")
print()

for pri in ("high", "medium"):
    sources = buckets.get(pri, {})
    total = sum(len(v) for v in sources.values())
    print(f"## {pri.upper()} ({total} PDFs)")
    print()
    for src in sorted(sources.keys(), key=lambda s: -len(sources[s])):
        items = sources[src]
        print(f"### {src} ({len(items)})")
        print()
        for it in items:
            print(f"- **{it['title']}**  ")
            print(f"  {it['ts']} | `{it['path']}`")
            if it["desc"]:
                print(f"  > {it['desc']}")
        print()
