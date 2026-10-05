"""Render the economic print embed body for a release with live agency
data, the calendar feed and a real Quick Takeaway, without posting.

Owner review tool (2026-09-30). Run on the worker, where the agency and
Gemini keys live, against a READ-ONLY database connection (the live
worker owns the writable one):

  /opt/venv/bin/python scripts/print_sample_run.py cpi 2026-08
  /opt/venv/bin/python scripts/print_sample_run.py pce 2026-08
  /opt/venv/bin/python scripts/print_sample_run.py jobs 2026-08
  /opt/venv/bin/python scripts/print_sample_run.py jobs 2026-09 2026-10-02   (past release date)
  /opt/venv/bin/python scripts/print_sample_run.py fomc <statement-url>

Prints the title, the body lines and the research notes the takeaway
saw. Never writes to the database or Discord: the spend-ledger write
fails against the read-only connection and is swallowed by design.
"""
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    key, arg = argv[0], argv[1]
    # the release date (YYYY-MM-DD) for a past print: revisions are read
    # as FRED showed them the day before it
    release_day = argv[2] if len(argv) > 2 else None
    import db
    from config import settings
    ro = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True, timeout=5,
                         check_same_thread=False)
    ro.row_factory = sqlite3.Row
    db.get_connection = lambda: ro          # read-only for everything below
    from report import print_watch as PW
    from report import print_takeaway as T

    spec = next(s for s in PW.SPECS if s.key == key)
    today = PW.datetime.now(PW._ET).date().isoformat()
    ff_rows = PW._ff_rows_for_day(today)
    if key == "fomc":
        parsed = PW.parse_fomc_statement(PW._http_text(arg))
        period = today[:7]
        rows = [PW._fomc_row(parsed, ff_rows, period)]
        computed = [f"Fed {PW._FED_VERBS.get(parsed['action'], parsed['action'])}"
                    + (f" · vote {parsed['vote']}" if parsed.get("vote") else "")]
        prev = PW.previous_post(spec.key, before=today)
        computed += [f"Statement change: {s}" for s in
                     PW.statement_changes((prev or {}).get("statement", ""), parsed.get("text", ""))]
    else:
        period = arg
        obs = PW.fetch_observations(spec.lines, force=True)
        print("series fetched:", sorted(obs))
        if not PW.release_ready(spec, obs, period):
            print(f"not ready: a core series lacks {period}")
            return 1
        rows = PW.build_rows(spec, obs, period, ff_rows)
        computed = []
        core = next((x for x in spec.lines if x.label.startswith("Core") and x.transform == "mom"), None)
        if core is not None:
            a3 = PW.annualized_3m(obs.get(core.series) or [], period)
            if a3 is not None:
                computed.append(f"{core.display.split(' (')[0]} 3-month annualized: {a3:.1f}%")
        before = PW.payroll_vintage(spec, period, release_day or today)
        rev = (PW.payroll_revisions(spec, obs, period, before)
               or PW.revision_line(spec, obs, period, PW.previous_post(spec.key, before=today)))
        if rev:
            computed.append(rev)
    title = PW.release_title(spec, period)
    month = PW.period_label(period).split()[0]
    research = T.research_for_release(key, period=month)
    takeaway = T.generate(key, title, rows, computed, research=research, period=month)
    body = PW.render_release(rows, computed=computed, takeaway=takeaway, source=PW.source_line(spec))
    print("=" * 72)
    print(title)
    print("-" * 72)
    print("\n".join(body))
    print("-" * 72)
    print(f"research notes seen: {len(research)}: {[n['source'] for n in research]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
