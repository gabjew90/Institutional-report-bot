"""Smoke: the 2026-08-12 MAIN EVENT / BRIEFS overhaul is wired end to end.

Research basis (scratchpad main_event_briefs_research.md): 12 QC reviews
split 8 yes / 4 not-yet on the miss-it test. Every "yes" traces to a
dated falsifiable stake, a named bank-vs-bank split, or the reader
tracking an outcome alongside the analyst. Every "not yet" traces to the
missing accountability loop, ~1 stake across 5 slots, or clone slots.

The overhaul adds, surgically:
  DRAFT  - Movement 3: corpus split (>=2/>=2) renders as NAMED debate
         - Movement 5: date the stake when a date exists
         - BRIEFS: three-beat spine + three named shapes + stake-or-demote
         - SETTLE resolved reads via the {open_reads_block} placeholder
         - positioning/flow/trigger data as must-surface
  AUDIT  - preserve stakes, settlements, named splits, positioning data,
           and brief shapes; settlement joins stance-inversion as the
           second sanctioned callback form
  ctx    - build_pulse_context supplies open_reads_block from pulse_leans

Also pins the removal of a leftover contradiction: DRAFT_USER carried
example closes ("The bias here is long $TLT.") that were exactly the
invented house positions banned two paragraphs above them.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_draft_carries_the_new_beats():
    import ai_analysis.prompts as p
    d = p.DRAFT_USER
    for marker, why in (
        ("staged as disagreement, not blended",
         "Movement 3 named-debate rule"),
        ("Date the stake when a date exists",
         "dated-stake rule"),
        ("SETTLE resolved reads",
         "settlement rule"),
        ("{open_reads_block}",
         "open-reads placeholder"),
        ("must carry a stake or it is a WATCH bullet",
         "stake-or-demote rule"),
        ("Data brief", "brief shape 1"),
        ("Debate brief", "brief shape 2"),
        ("Catalyst brief", "brief shape 3"),
        ("Positioning, flow and trigger data are must-surface",
         "positioning priority"),
    ):
        if marker not in d:
            _fail(f"DRAFT_USER missing the {why} ({marker!r})")
    _ok("DRAFT_USER carries all overhaul beats")


def test_invented_positions_only_appear_as_bad_examples():
    """The old 'positioning close' examples taught the banned behaviour as
    the ✅ default. They may survive ONLY as ❌ negative examples — every
    line containing one must be a line that starts by marking it bad."""
    import ai_analysis.prompts as p
    for phrase in ("The bias here is long $TLT",
                   "Net positioning view",
                   "Long $BNO captures it"):
        for line in p.DRAFT_USER.splitlines():
            if phrase in line and not line.strip().startswith("❌"):
                _fail(f"house-position phrasing outside a ❌ example: "
                      f"{line.strip()[:110]}")
    if "✅ Default (positioning lean only" in p.DRAFT_USER:
        _fail("the ✅ positioning-lean example block survives")
    _ok("house positions appear only as ❌ negative examples")


def test_settlement_is_the_exception_to_standalone():
    """Both standalone rules must carve out the settlement callback, or
    DRAFT is told to write it and told not to in the same prompt."""
    import ai_analysis.prompts as p
    if "with ONE exception: the settlement rule" not in p.DRAFT_USER:
        _fail("DRAFT standalone rule has no settlement carve-out")
    if "settlement sentences resolving a prior pulse's open read" \
            not in p.AUDIT_USER and \
            "settlement sentences resolving a prior pulse's open read" \
            not in p.AUDIT_SYSTEM:
        _fail("AUDIT standalone rule has no settlement carve-out — EDIT "
              "would delete the callback DRAFT was told to write")
    _ok("settlement is a sanctioned exception on both sides")


def test_audit_preserves_the_new_beats():
    import ai_analysis.prompts as p
    a = p.AUDIT_SYSTEM
    for marker, why in (
        ("Preserve the stakes and the settlements", "preservation block"),
        ("Preserve each brief's SHAPE", "shape preservation"),
        ("Named bank-vs-bank splits", "split preservation"),
        ("Positioning/trigger numbers", "positioning preservation"),
    ):
        if marker not in a:
            _fail(f"AUDIT_SYSTEM missing {why} ({marker!r})")
    _ok("AUDIT_SYSTEM preserves stakes, settlements, splits, shapes")


def test_ctx_supplies_open_reads():
    """build_pulse_context must define the placeholder's ctx key — its
    docstring promises keys map 1:1 to DRAFT_USER placeholders."""
    import inspect
    import report.synthesizer as syn
    src = inspect.getsource(syn.build_pulse_context)
    if '"open_reads_block"' not in src:
        _fail("build_pulse_context does not supply open_reads_block — the "
              "routine would ship the literal placeholder text")
    _ok("build_pulse_context supplies open_reads_block")


def test_open_reads_block_renders_from_leans():
    import sqlite3
    import db as dbmod
    from report.synthesizer import _render_open_reads_block
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    dbmod._init_schema(conn)
    dbmod._conn = conn
    # empty state
    if _render_open_reads_block("2026-08-13") != "(none)":
        _fail("empty pulse_leans must render '(none)'")
    dbmod.upsert_pulse_leans("2026-08-10", [
        {"instrument": "SLV", "direction": "long",
         "context": "Long $SLV · $64 hold is the trigger"}])
    dbmod.upsert_pulse_leans("2026-08-12", [
        {"instrument": "QQQ", "direction": "short",
         "context": "$QQQ puts · credit repricing AI"}])
    out = _render_open_reads_block("2026-08-13")
    if "LONG $SLV (since 2026-08-10)" not in out:
        _fail(f"open read missing or mis-rendered:\n{out}")
    if "$64 hold" not in out:
        _fail(f"read context lost:\n{out}")
    if out.splitlines()[0].find("SLV") < 0:
        _fail(f"oldest read must lead (most overdue for settlement):\n{out}")
    _ok("open reads render oldest-first with direction, date and context")


def test_placeholder_is_format_style_not_composition():
    """{open_reads_block} is a routine-substituted placeholder like
    {today}, NOT a <<...>> composition token — dump_prompts must not
    reject it."""
    import subprocess
    import tempfile
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with tempfile.TemporaryDirectory() as td:
        r = subprocess.run(
            [sys.executable, os.path.join(repo, "scripts", "dump_prompts.py"),
             td],
            capture_output=True, text=True, cwd=repo,
            env={**os.environ, "PYTHONPATH": repo,
                 "PYTHONIOENCODING": "utf-8"},
        )
        if r.returncode != 0:
            _fail(f"dump_prompts rejects the new placeholder: "
                  f"{r.stderr[:300]}")
    _ok("dump_prompts accepts the new placeholder")


if __name__ == "__main__":
    test_draft_carries_the_new_beats()
    test_invented_positions_only_appear_as_bad_examples()
    test_settlement_is_the_exception_to_standalone()
    test_audit_preserves_the_new_beats()
    test_ctx_supplies_open_reads()
    test_open_reads_block_renders_from_leans()
    test_placeholder_is_format_style_not_composition()
    print("\nAll MAIN EVENT / BRIEFS contract smoke tests passed.")
