"""Smoke test: synthesizer injects recent pulse titles + a TITLE-NOVELTY
RULE so DRAFT stops recycling catchphrases like 'AI cracks'.

Background: 2026-06-04 / 06-05 / 06-08 pulses all used 'AI cracks' as
the second clause of the title:
  - 06-04: 'Hormuz softens, AI cracks'
  - 06-05: 'Oil bid, AI cracks'
  - 06-08: 'Jobs hot, AI cracks'

Three consecutive uses across four trading days reads as the bot
recycling its catchphrase, not as the news genuinely re-confirming
the same framing.

Fix: extend the existing prev_pulse block in the synthesizer prompt
to include "RECENT PULSE TITLES TO AVOID RECYCLING" with a binding
TITLE-NOVELTY RULE that says: do not reuse any 2-4 word phrase that
appears verbatim in 2+ of the recent titles.

Source of truth: new db helper `get_recent_daily_pulse_titles(limit)`
that pulls the H1 from the last N `report_type='daily'` rows.

This smoke covers:
  - db.get_recent_daily_pulse_titles signature + ordering (newest
    first) + frontmatter stripping + H1 extraction
  - synthesizer.py calls the helper when building prev_context
  - The prev_context prompt contains a TITLE-NOVELTY RULE block
  - The rule references the 06-04/06-05/06-08 'AI cracks' pattern
    as the concrete anchor
  - Binding language ('binding', 'must NOT') so the model treats it
    as a hard rule
"""

import inspect
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _with_fresh_db(fn):
    """Run `fn` with a fresh in-memory db (closes + unlinks after)."""
    import db
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    old_path = os.environ.get("DB_PATH")
    os.environ["DB_PATH"] = tmp.name
    try:
        from config import settings as _settings
        _settings.db_path = tmp.name
        db._conn = None
        db.get_connection()  # lazy schema init
        fn(db)
    finally:
        try:
            if db._conn is not None:
                db._conn.close()
        except Exception:
            pass
        db._conn = None
        if old_path is not None:
            os.environ["DB_PATH"] = old_path
        try:
            os.unlink(tmp.name)
        except PermissionError:
            pass


def test_db_get_recent_titles_signature_and_ordering():
    """Insert 6 daily pulses with distinct H1 titles, verify the helper
    returns the most recent N in newest-first order."""
    import db
    titles_inserted = [
        "Jobs hot, AI cracks",       # newest
        "Oil bid, AI cracks",
        "Hormuz softens, AI cracks",
        "ECB hike, dollar bid",
        "Crypto cracks, equities hold",
        "VIX uncage",                # oldest
    ]

    def _run(db_mod):
        for i, title in enumerate(reversed(titles_inserted)):
            md = f"# {title}\n\n## 1. RECAP\nbody body body."
            db_mod.get_connection().execute(
                "INSERT INTO daily_reports "
                "(report_date, report_markdown, report_json, pdf_count, "
                "report_type, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (f"2026-06-0{i+1}", md, "{}", 50, "daily",
                 f"2026-06-0{i+1}T13:00:00"),
            )
            db_mod.get_connection().commit()
        out = db_mod.get_recent_daily_pulse_titles(limit=5)
        # Newest first, limit 5 — drops the oldest "VIX uncage"
        assert out == titles_inserted[:5], (
            f"expected {titles_inserted[:5]}, got {out}"
        )

    _with_fresh_db(_run)
    _ok("get_recent_daily_pulse_titles: limit + newest-first ordering "
        "+ H1 extraction work")


def test_db_strips_frontmatter_before_extracting_title():
    """Pulse markdown commonly starts with a `---` frontmatter block
    (pdf_count, input_tokens, etc.). The helper must skip that and
    extract the H1 from the body."""
    import db

    def _run(db_mod):
        md_with_frontmatter = (
            "---\n"
            "pdf_count: 109\n"
            "dumped_at_utc: 2026-06-08T12:58:04Z\n"
            "---\n\n"
            "# Jobs hot, AI cracks\n\n"
            "## 1. RECAP\n..."
        )
        db_mod.get_connection().execute(
            "INSERT INTO daily_reports "
            "(report_date, report_markdown, report_json, pdf_count, "
            "report_type, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("2026-06-08", md_with_frontmatter, "{}", 109, "daily",
             "2026-06-08T13:00:00"),
        )
        db_mod.get_connection().commit()
        out = db_mod.get_recent_daily_pulse_titles(limit=3)
        assert out == ["Jobs hot, AI cracks"], (
            f"frontmatter must be stripped before H1 extraction, "
            f"got: {out}"
        )

    _with_fresh_db(_run)
    _ok("frontmatter stripped before H1 extraction (real pulse shape)")


def test_synthesizer_calls_recent_titles_helper():
    """Source-level anchor: synthesizer.py must call
    get_recent_daily_pulse_titles when building prev_context."""
    from report import synthesizer
    src = inspect.getsource(synthesizer)
    assert "get_recent_daily_pulse_titles" in src, (
        "synthesizer.py must call db.get_recent_daily_pulse_titles to "
        "build the title-novelty block"
    )
    _ok("synthesizer.py calls db.get_recent_daily_pulse_titles")


def test_synthesizer_prev_context_has_title_novelty_block():
    """Source-level anchor: synthesizer's prev_context construction
    must include the 'TITLE-NOVELTY RULE' header + 'RECENT PULSE
    TITLES' section + binding language."""
    from report import synthesizer
    src = inspect.getsource(synthesizer)
    assert "RECENT PULSE TITLES TO AVOID RECYCLING" in src, (
        "prev_context must include a 'RECENT PULSE TITLES TO AVOID "
        "RECYCLING' header so the model recognizes the dataset"
    )
    assert "TITLE-NOVELTY RULE" in src, (
        "prev_context must include the named 'TITLE-NOVELTY RULE' "
        "header so the model treats it as a binding rule"
    )
    assert "binding" in src.lower(), (
        "title-novelty rule must be marked 'binding'"
    )
    _ok("prev_context contains 'RECENT PULSE TITLES' + named "
        "'TITLE-NOVELTY RULE' marked binding")


def test_title_novelty_rule_references_ai_cracks_pattern():
    """The rule must quote the concrete 06-04 / 06-05 / 06-08 'AI
    cracks' recycling pattern as a pattern anchor — abstract 'don't
    recycle catchphrases' rules don't land as well as 'here's the
    exact mistake.'"""
    from report import synthesizer
    src = inspect.getsource(synthesizer)
    assert "AI cracks" in src, (
        "rule must quote the specific 'AI cracks' catchphrase that "
        "recurred"
    )
    for date in ("2026-06-04", "2026-06-05", "2026-06-08"):
        assert date in src, (
            f"rule should anchor on the {date} pulse as one of the "
            f"3 recurrence cases"
        )
    _ok("rule references 06-04 / 06-05 / 06-08 'AI cracks' recurrence "
        "as concrete pattern anchor")


def test_title_novelty_rule_specifies_recurrence_threshold():
    """The rule must specify HOW MANY recurrences trigger the ban
    (2+ titles, otherwise the model has no actionable bar). Without
    the threshold the rule reads as 'be creative' which doesn't bind."""
    from report import synthesizer
    src = inspect.getsource(synthesizer)
    # Look for explicit recurrence count
    threshold_phrases = [
        "2 or more", "2+", "two or more",
        "appears verbatim in 2",
    ]
    matched = [p for p in threshold_phrases if p in src]
    assert matched, (
        f"rule must specify a recurrence threshold (2+ titles), got "
        f"no matches for: {threshold_phrases}"
    )
    _ok(f"rule specifies recurrence threshold: {matched}")


if __name__ == "__main__":
    print("=== Pulse title-novelty injection smoke ===")
    test_db_get_recent_titles_signature_and_ordering()
    test_db_strips_frontmatter_before_extracting_title()
    test_synthesizer_calls_recent_titles_helper()
    test_synthesizer_prev_context_has_title_novelty_block()
    test_title_novelty_rule_references_ai_cracks_pattern()
    test_title_novelty_rule_specifies_recurrence_threshold()
    print("\nALL TITLE-NOVELTY INJECTION SMOKE TESTS PASS")
