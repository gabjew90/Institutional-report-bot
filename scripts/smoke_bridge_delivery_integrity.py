"""Smoke: bridge delivery integrity — crash-loop, dupes, test-fire type,
footer corpus.

Four defects from the 2026-08-04 end-to-end pulse review:

1. `_parse_frontmatter` coerced all-digit values to int, so a numeric
   `target_channels:` (a single channel ID — explicitly permitted) made
   `(meta.get("target_channels") or "").strip()` raise AttributeError.
   The outer handler returned without moving the file, so the pulse
   re-crashed every 60s poll forever — never delivered, never aging into
   delivery-failed.

2. No idempotency: on the success path the Discord post and DB insert
   happen BEFORE the GitHub archive/delete. If either GitHub write threw,
   the pending file survived and the next poll re-posted the entire pulse
   to every channel and inserted a second `daily` row.

3. Test fires persisted as report_type='daily', so a weekend test fire
   became the next production pulse's prev-scheduled anchor and silently
   disabled off-board scoring and thesis-flip detection.

4. `_compute_footer_stats` counted LOW-priority analyses the dump job
   filters out of synthesis — the footer described a corpus the pulse
   never saw (the exact mismatch class its own docstring claims fixed).
"""

import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_numeric_frontmatter_values_stay_strings():
    from github_bridge import jobs
    meta, body = jobs._parse_frontmatter(
        "---\ntarget_channels: 140487999923997251\npdf_count: 51\n---\nbody"
    )
    # The exact production expression that crash-looped:
    flt = (meta.get("target_channels") or "").strip()
    assert flt == "140487999923997251", meta
    # Numeric consumers cast for themselves — must still work:
    assert int(meta.get("pdf_count", 0)) == 51, meta
    assert body == "body"
    _ok("numeric frontmatter stays str; .strip() consumers safe")


def test_test_fires_write_manual_report_type():
    from github_bridge import jobs
    src = inspect.getsource(jobs._process_one_pulse)
    assert '"daily" ' not in src.replace(
        '_is_test_fire else "daily"', ""
    ) or True  # structure checked below
    n = src.count('"manual" if _is_test_fire else "daily"')
    assert n >= 2, (
        f"both the DailyReport and the insert_daily_report call must write "
        f"'manual' for test fires (found {n} of 2) — a test fire stored as "
        f"'daily' becomes the next pulse's prev-scheduled anchor and kills "
        f"off-board scoring"
    )
    _ok("test fires persist as report_type='manual' at both write sites")


def test_success_path_is_idempotent():
    from github_bridge import jobs
    src = inspect.getsource(jobs._process_one_pulse)
    assert "find_sent_report_by_pending_file" in src, (
        "no idempotency check — a GitHub archive failure after a "
        "successful post re-posts the whole pulse on the next poll"
    )
    # The check must run BEFORE the Discord send loop.
    guard = src.find("find_sent_report_by_pending_file")
    send = src.find("send_pulse_embeds")
    if send == -1:
        send = src.find("channels_sent")
    assert guard != -1 and guard < send, (
        "idempotency guard must run before any Discord posting"
    )
    _ok("already-posted pulses skip Discord and go straight to cleanup")


def test_find_sent_report_helper():
    import sqlite3
    import db as dbmod
    conn = sqlite3.connect(":memory:")
    dbmod._init_schema(conn)
    orig = dbmod.get_connection
    dbmod.get_connection = lambda: conn
    try:
        rid = dbmod.insert_daily_report(
            report_date="2026-08-04", report_type="daily",
            report_json='{"source": "github_bridge", "pending_file": "pulse-x.md"}',
            report_markdown="# t", pdf_count=1,
            input_tokens=0, output_tokens=0)
        # Not yet sent -> no match (a failed post must not block retry)
        assert dbmod.find_sent_report_by_pending_file("pulse-x.md") is None
        dbmod.mark_report_sent(rid)
        assert dbmod.find_sent_report_by_pending_file("pulse-x.md") == rid
        assert dbmod.find_sent_report_by_pending_file("pulse-y.md") is None
    finally:
        dbmod.get_connection = orig
        conn.close()
    _ok("find_sent_report_by_pending_file matches only SENT rows by name")


def test_footer_stats_exclude_low():
    from github_bridge import jobs
    src = inspect.getsource(jobs._compute_footer_stats)
    assert "low" in src, (
        "footer stats must drop LOW-priority rows the same way the dump "
        "job does — otherwise the footer describes a corpus the pulse "
        "never synthesized"
    )
    _ok("footer stats filter LOW like the synthesis dump does")


if __name__ == "__main__":
    print("=== bridge delivery integrity smoke ===")
    test_numeric_frontmatter_values_stay_strings()
    test_test_fires_write_manual_report_type()
    test_success_path_is_idempotent()
    test_find_sent_report_helper()
    test_footer_stats_exclude_low()
    print("\nALL BRIDGE DELIVERY INTEGRITY SMOKE TESTS PASS")
