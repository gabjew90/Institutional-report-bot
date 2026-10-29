"""Render the economic print alert for a release from live agency data
and the calendar feed, without posting.

Owner review tool (2026-09-30, alert layout since 2026-10-28). Run on the
worker, where the agency keys and the print-alert ledger live:

  /opt/venv/bin/python scripts/print_sample_run.py cpi 2026-09
  /opt/venv/bin/python scripts/print_sample_run.py pce 2026-08
  /opt/venv/bin/python scripts/print_sample_run.py jobs 2026-09 2026-10-02   (past release date)
  /opt/venv/bin/python scripts/print_sample_run.py fomc <statement-url>

Prints the message text, the embed lines and the footer. Reads the
ledger and never writes it, never posts, and makes no model call.
"""
from __future__ import annotations

import os
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
    from report import print_data as PD
    from report import print_watch as PW

    spec = next(s for s in PW.SPECS if s.key == key)
    today = release_day or PW.datetime.now(PW._ET).date().isoformat()
    ff_rows = PW._ff_rows_for_day(today)
    prev = PW.previous_post(spec.key, before=today)
    if key == "fomc":
        parsed = PW.parse_fomc_statement(PW._http_text(arg))
        parsed["url"] = arg
        from report import fed_projections as FP
        proj = FP.fetch_projections(today) if FP.is_projection_meeting(today) else None
        print("projections:", proj)
        rel = PD.fomc_release(parsed, ff_rows, prev, today, proj)
    else:
        period = arg
        obs = PW.fetch_observations(spec.lines, force=True)
        print("series fetched:", sorted(obs))
        if not PW.release_ready(spec, obs, period):
            print(f"not ready: a core series lacks {period}")
            return 1
        vintage = PW.payroll_vintage(spec, period, today)
        rel = PD.data_release(spec, obs, period, ff_rows, prev, vintage)
    alert, v = PW.compose_alert(spec, rel, today, spec.release_et)
    print("=" * 72)
    print(alert.content)
    print("-" * 72)
    print("\n".join(alert.lines))
    print("-" * 72)
    print(alert.footer)
    print(f"signals: {v.signals}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
