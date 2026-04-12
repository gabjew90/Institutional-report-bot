"""CLI for browsing the SQLite DB.

Usage:
    py inspect_db.py                        # Summary overview
    py inspect_db.py pdfs                   # List recent PDFs
    py inspect_db.py pdfs --priority high   # Filter by priority
    py inspect_db.py pdfs --status FAILED   # Filter by status
    py inspect_db.py pdfs --search goldman  # Filter by filename substring
    py inspect_db.py analysis <pdf_id>      # Full analysis for one PDF
    py inspect_db.py reports                # List daily reports
    py inspect_db.py report <report_id>     # Full markdown of a daily report
    py inspect_db.py log                    # Recent processing events
"""

import argparse
import io
import json
import sys
from datetime import date

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import db


def _trunc(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


def cmd_summary():
    conn = db.get_connection()
    print("=" * 70)
    print("DATABASE SUMMARY")
    print("=" * 70)

    # Row counts
    print("\nRow counts:")
    for t in ["pdf_files", "pdf_analyses", "daily_reports", "processing_log"]:
        c = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t:20s} {c}")

    # PDFs by status
    print("\nPDFs by status:")
    for row in conn.execute("SELECT status, COUNT(*) c FROM pdf_files GROUP BY status ORDER BY c DESC"):
        print(f"  {row['status']:15s} {row['c']}")

    # Analyses by priority
    print("\nAnalyses by priority:")
    for row in conn.execute("SELECT priority, COUNT(*) c FROM pdf_analyses GROUP BY priority ORDER BY c DESC"):
        print(f"  {row['priority']:15s} {row['c']}")

    # Analyses by source (top 10)
    print("\nTop sources (from analysis_json):")
    rows = conn.execute("SELECT analysis_json FROM pdf_analyses WHERE analysis_json IS NOT NULL").fetchall()
    src_counts: dict[str, int] = {}
    for r in rows:
        try:
            data = json.loads(r["analysis_json"])
            src = data.get("source", "Unknown")
            src_counts[src] = src_counts.get(src, 0) + 1
        except json.JSONDecodeError:
            pass
    for src, c in sorted(src_counts.items(), key=lambda x: -x[1])[:10]:
        print(f"  {src:30s} {c}")

    # Token usage
    print("\nToken usage (pdf_analyses):")
    row = conn.execute(
        "SELECT COALESCE(SUM(input_tokens_used),0) i, COALESCE(SUM(output_tokens_used),0) o FROM pdf_analyses"
    ).fetchone()
    print(f"  input:  {row['i']:>12,}")
    print(f"  output: {row['o']:>12,}")

    # Dropbox cursor
    print("\nDropbox state:")
    row = conn.execute("SELECT cursor, last_poll_at FROM dropbox_state WHERE id = 1").fetchone()
    if row:
        cursor = row["cursor"]
        print(f"  cursor:       {'(set)' if cursor else '(unset)'}")
        print(f"  last_poll_at: {row['last_poll_at'] or '(never)'}")


def cmd_pdfs(args):
    conn = db.get_connection()
    query = """SELECT pf.id, pf.file_name, pf.status, pf.priority,
                      pf.dropbox_modified_at, pf.file_size_bytes,
                      pa.id as analysis_id
               FROM pdf_files pf
               LEFT JOIN pdf_analyses pa ON pa.pdf_file_id = pf.id
               WHERE 1=1"""
    params = []
    if args.priority:
        query += " AND pf.priority = ?"
        params.append(args.priority)
    if args.status:
        query += " AND pf.status = ?"
        params.append(args.status)
    if args.search:
        query += " AND pf.file_name LIKE ?"
        params.append(f"%{args.search}%")
    query += " ORDER BY pf.dropbox_modified_at DESC LIMIT ?"
    params.append(args.limit)

    rows = conn.execute(query, params).fetchall()

    print(f"{'ID':>5}  {'PRIORITY':8}  {'STATUS':10}  {'UPLOADED':19}  {'SIZE':>8}  FILE")
    print("-" * 120)
    for r in rows:
        size_kb = (r["file_size_bytes"] or 0) / 1024
        uploaded = (r["dropbox_modified_at"] or "")[:19].replace("T", " ")
        print(f"{r['id']:>5}  {(r['priority'] or '-'):8}  {r['status']:10}  {uploaded:19}  {size_kb:>6.0f}kb  {_trunc(r['file_name'], 60)}")

    print(f"\n{len(rows)} rows")


def cmd_analysis(pdf_id: int):
    conn = db.get_connection()
    row = conn.execute(
        """SELECT pa.*, pf.file_name, pf.dropbox_path, pf.dropbox_modified_at
           FROM pdf_analyses pa JOIN pdf_files pf ON pa.pdf_file_id = pf.id
           WHERE pf.id = ?""",
        (pdf_id,),
    ).fetchone()
    if not row:
        print(f"No analysis found for pdf_id={pdf_id}")
        return

    print(f"File:     {row['file_name']}")
    print(f"Path:     {row['dropbox_path']}")
    print(f"Uploaded: {row['dropbox_modified_at']}")
    print(f"Priority: {row['priority']}")
    print(f"Pages:    {row['pages_analyzed']}/{row['total_pages']} analyzed")
    print(f"Tokens:   {row['input_tokens_used']:,} in / {row['output_tokens_used']:,} out")
    print(f"Model:    {row['model_used']} ({row['analysis_duration_seconds']:.1f}s)")
    print()

    if row["triage_json"]:
        print("=" * 70)
        print("TRIAGE")
        print("=" * 70)
        try:
            t = json.loads(row["triage_json"])
            print(f"  priority:    {t.get('priority')}")
            print(f"  report_type: {t.get('report_type')}")
            print(f"  tickers:     {t.get('key_tickers', [])}")
            print(f"  summary:     {t.get('summary')}")
        except json.JSONDecodeError:
            print(row["triage_json"])

    if row["analysis_json"]:
        print()
        print("=" * 70)
        print("ANALYSIS")
        print("=" * 70)
        try:
            a = json.loads(row["analysis_json"])
            print(f"  source:      {a.get('source')}")
            print(f"  title:       {a.get('title')}")
            print(f"  report_type: {a.get('report_type')}")
            for field in ["key_insights", "earnings_insights", "crypto_views",
                          "risk_factors", "charts_described"]:
                items = a.get(field, [])
                if items:
                    print(f"\n  {field}:")
                    for item in items:
                        print(f"    - {item}")
            if a.get("market_movers"):
                print("\n  market_movers:")
                for mm in a["market_movers"]:
                    print(f"    - {mm.get('ticker')}: {mm.get('action')} ({mm.get('rating')}) @ {mm.get('price_target')} — {mm.get('rationale')}")
            if a.get("macro_indicators"):
                print("\n  macro_indicators:")
                for mi in a["macro_indicators"]:
                    print(f"    - {mi.get('indicator')}: {mi.get('reading')} — {mi.get('interpretation')}")
            if a.get("trade_ideas"):
                print("\n  trade_ideas:")
                for ti in a["trade_ideas"]:
                    print(f"    - {ti.get('description')}: {ti.get('rationale')} (risk: {ti.get('risk')})")
        except json.JSONDecodeError:
            print(row["analysis_json"])


def cmd_reports():
    conn = db.get_connection()
    rows = conn.execute(
        """SELECT id, report_date, report_type, pdf_count,
                  input_tokens_used, output_tokens_used,
                  discord_sent_at, created_at
           FROM daily_reports ORDER BY created_at DESC LIMIT 20"""
    ).fetchall()
    if not rows:
        print("No daily reports in DB yet.")
        return

    print(f"{'ID':>4}  {'DATE':10}  {'TYPE':8}  {'PDFS':>5}  {'TOKENS (i/o)':>18}  {'SENT':19}  CREATED")
    print("-" * 100)
    for r in rows:
        sent = (r["discord_sent_at"] or "-")[:19].replace("T", " ")
        created = (r["created_at"] or "")[:19].replace("T", " ")
        tokens = f"{r['input_tokens_used']:,}/{r['output_tokens_used']:,}"
        print(f"{r['id']:>4}  {r['report_date']:10}  {r['report_type']:8}  {r['pdf_count']:>5}  {tokens:>18}  {sent:19}  {created}")


def cmd_report(report_id: int):
    conn = db.get_connection()
    row = conn.execute("SELECT * FROM daily_reports WHERE id = ?", (report_id,)).fetchone()
    if not row:
        print(f"No report with id={report_id}")
        return

    print(f"Date:    {row['report_date']}")
    print(f"Type:    {row['report_type']}")
    print(f"PDFs:    {row['pdf_count']}")
    print(f"Tokens:  {row['input_tokens_used']:,} in / {row['output_tokens_used']:,} out")
    print(f"Sent:    {row['discord_sent_at'] or 'not sent'}")
    print("=" * 70)
    print(row["report_markdown"])


def cmd_log(args):
    conn = db.get_connection()
    rows = conn.execute(
        """SELECT pl.*, pf.file_name
           FROM processing_log pl
           LEFT JOIN pdf_files pf ON pl.pdf_file_id = pf.id
           ORDER BY pl.created_at DESC LIMIT ?""",
        (args.limit,),
    ).fetchall()

    print(f"{'TIME':19}  {'EVENT':12}  {'STATUS':10}  FILE / DETAILS")
    print("-" * 100)
    for r in rows:
        t = (r["created_at"] or "")[:19].replace("T", " ")
        file_or_details = r["file_name"] or (r["details"] or "")[:60]
        print(f"{t:19}  {r['event_type']:12}  {r['status']:10}  {_trunc(file_or_details, 60)}")


def main():
    parser = argparse.ArgumentParser(description="Inspect the analyzer DB")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("summary", help="Overview stats (default)")

    p_pdfs = sub.add_parser("pdfs", help="List recent PDFs")
    p_pdfs.add_argument("--priority", choices=["high", "medium", "low"])
    p_pdfs.add_argument("--status", choices=["DOWNLOADED", "PROCESSING", "PROCESSED", "FAILED"])
    p_pdfs.add_argument("--search", type=str, help="Filename substring")
    p_pdfs.add_argument("--limit", type=int, default=30)

    p_an = sub.add_parser("analysis", help="Show full analysis for a PDF")
    p_an.add_argument("pdf_id", type=int)

    sub.add_parser("reports", help="List daily reports")

    p_r = sub.add_parser("report", help="Show markdown of a daily report")
    p_r.add_argument("report_id", type=int)

    p_log = sub.add_parser("log", help="Recent processing events")
    p_log.add_argument("--limit", type=int, default=30)

    args = parser.parse_args()
    db.get_connection()

    if args.cmd == "pdfs":
        cmd_pdfs(args)
    elif args.cmd == "analysis":
        cmd_analysis(args.pdf_id)
    elif args.cmd == "reports":
        cmd_reports()
    elif args.cmd == "report":
        cmd_report(args.report_id)
    elif args.cmd == "log":
        cmd_log(args)
    else:
        cmd_summary()


if __name__ == "__main__":
    main()
