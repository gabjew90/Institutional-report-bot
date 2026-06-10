"""Smoke test: format-overhaul Phase 1 — WHAT CHANGED + TRADE BOARD.

Design: both sections are assembled DETERMINISTICALLY by the bridge at
post time and injected into the final pulse markdown — the LLM never
touches them. State lives in db.pulse_state (theme/call snapshots) and
db.pulse_leans (leans tracked across days).

Covers:
  - extract_state_from_ctx: compact snapshot from a pulse context
  - compute_what_changed: stance flips / fresh HC calls / new themes /
    faded themes; empty on baseline day
  - extract_leans_from_markdown: closing-paragraph leans only, puts
    flip direction, dedup
  - render_trade_board: NEW vs dN, monospace block, empty when no rows
  - inject_sections: placement (after RECAP / before WATCH) + idempotency
  - DB round-trip: candidate save → stamp (idempotent) → prev lookup;
    lean upsert (new / re-affirm) → board → age-out
  - formatter colors + fragment class hooks for the new sections
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


# =====================================================================
# State extraction + diff
# =====================================================================

def _ctx(themes, hc_calls_analyses):
    return {
        "theme_map": themes,
        "analyses_json": json.dumps(hc_calls_analyses),
    }


def test_extract_state_from_ctx():
    from report.pulse_sections import extract_state_from_ctx
    ctx = _ctx(
        {
            "ai positioning": {"banks": 20, "supportive": 10,
                               "skeptical": 4, "high_conviction": 5},
            "boj policy pivot": {"banks": 6, "supportive": 2,
                                 "skeptical": 0, "high_conviction": 2},
        },
        [
            {"source": "Citi", "market_movers": [
                {"ticker": "NVDA", "conviction": "high",
                 "action": "reiterate Buy", "price_target": "$300"},
                {"ticker": "GIVN.S", "conviction": "high",
                 "action": "upgrade", "price_target": "CHF3300"},
                {"ticker": "AAPL", "conviction": "medium",
                 "action": "reiterate", "price_target": "$250"},
            ]},
        ],
    )
    state = extract_state_from_ctx(ctx)
    labels = [t["label"] for t in state["themes"]]
    assert labels[0] == "ai positioning", labels
    assert state["themes"][1]["hc"] == 2
    tickers = [c["ticker"] for c in state["hc_calls"]]
    assert tickers == ["NVDA"], (
        f"only US-tradable high-conviction calls belong in state, got "
        f"{tickers}"
    )
    _ok("extract_state_from_ctx: themes ranked + HC calls filtered to "
        "US-tradable high conviction")


def test_compute_what_changed_categories():
    from report.pulse_sections import compute_what_changed
    prev = {
        "themes": [
            {"label": "hormuz oil", "banks": 12, "sup": 1, "skep": 4, "hc": 0},
            {"label": "old fading theme", "banks": 8, "sup": 2, "skep": 0, "hc": 0},
        ],
        "hc_calls": [{"source": "Citi", "ticker": "NVDA",
                      "action": "reiterate Buy", "pt": "$300"}],
    }
    today = {
        "themes": [
            # stance flip: was net skeptical (1-4), now net supportive (5-1)
            {"label": "hormuz oil", "banks": 12, "sup": 5, "skep": 1, "hc": 0},
            # new multi-bank theme
            {"label": "boj policy pivot", "banks": 6, "sup": 2, "skep": 0, "hc": 2},
        ],
        "hc_calls": [
            {"source": "Citi", "ticker": "NVDA",
             "action": "reiterate Buy", "pt": "$300"},        # carried — no bullet
            {"source": "Goldman Sachs", "ticker": "MU",
             "action": "price_target_change", "pt": "$900"},  # fresh
        ],
    }
    bullets = compute_what_changed(prev, today)
    joined = " | ".join(bullets)
    assert "Stance flip" in joined and "hormuz oil" in joined, bullets
    assert "Fresh high-conviction" in joined and "$MU" in joined, bullets
    assert "New theme" in joined and "boj policy pivot" in joined, bullets
    assert "Faded" in joined and "old fading theme" in joined, bullets
    # The carried NVDA call must NOT appear as fresh
    assert joined.count("NVDA") == 0, bullets
    _ok("compute_what_changed: flip + fresh-HC + new + faded all "
        "detected; carried call suppressed")


def test_what_changed_baseline_day_empty():
    from report.pulse_sections import compute_what_changed, render_what_changed
    assert compute_what_changed(None, {"themes": [], "hc_calls": []}) == []
    assert render_what_changed([]) == "", (
        "baseline day renders NOTHING — no empty-section noise"
    )
    _ok("baseline day: no bullets, no section")


# =====================================================================
# Lean extraction + board rendering
# =====================================================================

_PULSE_MD = """# Test pulse

## 1. RECAP

Stuff happened today.

## 2. INSIGHTS & ALPHA

### AI wobble theme

*Punchline.*

- evidence bullet mentioning long $NVDA support in the mid-body

Mechanism paragraph that says you could buy $AAPL in theory here.

Stay long the build-out through the broader names. Long $SOXX on the broadening thesis, sized to ride the unwind out.

### BoJ theme

*Punchline two.*

- bullet

The cost of being wrong here is small.

Own some protection and trim the crowded exposure. Long $TLT calls into the June 16 decision.

## 3. WHAT TO WATCH

- stuff
"""


def test_extract_leans_closing_paragraph_only():
    from report.pulse_sections import extract_leans_from_markdown
    leans = extract_leans_from_markdown(_PULSE_MD)
    by_inst = {l["instrument"]: l for l in leans}
    assert "SOXX" in by_inst and by_inst["SOXX"]["direction"] == "long", leans
    assert "TLT calls" in by_inst and by_inst["TLT calls"]["direction"] == "long", leans
    # Mid-body mentions must NOT extract
    assert "NVDA" not in by_inst, (
        f"mid-body evidence mention extracted as a lean: {leans}"
    )
    assert "AAPL" not in by_inst, leans
    _ok("lean extraction: closing-paragraph only, mid-body mentions "
        "ignored, options qualifier captured")


def test_puts_flip_direction():
    from report.pulse_sections import extract_leans_from_markdown
    md = """## 2. INSIGHTS & ALPHA

### Rates theme

*Punch.*

Body paragraph.

The hedge is cheap. Own $TLT puts into the print.

## 3. WHAT TO WATCH
- x
"""
    leans = extract_leans_from_markdown(md)
    assert len(leans) == 1, leans
    assert leans[0]["instrument"] == "TLT puts"
    assert leans[0]["direction"] == "short", (
        f"'own $TLT puts' is a SHORT-rates expression, got {leans[0]}"
    )
    _ok("'own $X puts' flips direction to short")


def test_render_trade_board_new_vs_live():
    from report.pulse_sections import render_trade_board
    rows = [
        {"instrument": "SOXX", "direction": "long",
         "first_seen_date": "2026-06-10", "last_seen_date": "2026-06-10",
         "context_snippet": "broadening thesis"},
        {"instrument": "BNO", "direction": "long",
         "first_seen_date": "2026-06-08", "last_seen_date": "2026-06-10",
         "context_snippet": "oil floor vs futures fade"},
    ]
    board = render_trade_board(rows, "2026-06-10")
    assert "## TRADE BOARD" in board
    assert "```" in board, "must render as a monospace block (Discord " \
        "embeds don't render markdown tables)"
    assert "NEW " in board and "$SOXX" in board
    assert "d3" in board and "$BNO" in board, board
    assert render_trade_board([], "2026-06-10") == ""
    _ok("trade board: NEW vs dN day counts, monospace block, empty "
        "when no rows")


# =====================================================================
# Injection
# =====================================================================

def test_inject_sections_placement_and_idempotency():
    from report.pulse_sections import inject_sections
    wc = "## WHAT CHANGED\n\n- **New theme:** boj policy pivot (6 banks)\n"
    board = "## TRADE BOARD\n\n```\nNEW  LONG  $SOXX\n```\n"
    out = inject_sections(_PULSE_MD, wc, board)
    # Placement: WHAT CHANGED between RECAP and INSIGHTS
    assert out.index("## 1. RECAP") < out.index("## WHAT CHANGED") < \
           out.index("## 2. INSIGHTS"), "WHAT CHANGED must sit after RECAP"
    # TRADE BOARD between INSIGHTS and WATCH
    assert out.index("## 2. INSIGHTS") < out.index("## TRADE BOARD") < \
           out.index("## 3. WHAT TO WATCH"), "BOARD must sit before WATCH"
    # Idempotency — re-injection is a no-op
    again = inject_sections(out, wc, board)
    assert again == out, "re-injection must not duplicate sections"
    _ok("injection: correct placement + idempotent on retry")


def test_replace_body_after_frontmatter():
    from report.pulse_sections import replace_body_after_frontmatter
    raw = "---\npdf_count: 99\n---\n\n# Old body\n"
    out = replace_body_after_frontmatter(raw, "# New body\n")
    assert out.startswith("---\npdf_count: 99\n---")
    assert "# New body" in out and "# Old body" not in out
    # No frontmatter → new body verbatim
    assert replace_body_after_frontmatter("# Plain\n", "# New\n") == "# New\n"
    _ok("frontmatter-preserving body replacement")


# =====================================================================
# DB round-trip
# =====================================================================

def test_db_state_and_leans_roundtrip():
    import db
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    try:
        from config import settings as _settings
        _settings.db_path = tmp.name
        db._conn = None
        db.get_connection()

        # State: candidate → stamp → prev lookup
        db.save_pulse_state_candidate('{"themes": [], "hc_calls": []}',
                                      "2026-06-09T13:00:00Z")
        s1 = db.stamp_pulse_state_for_date("2026-06-09")
        assert s1 is not None
        db.save_pulse_state_candidate('{"themes": [{"label": "x"}], "hc_calls": []}',
                                      "2026-06-10T13:00:00Z")
        s2 = db.stamp_pulse_state_for_date("2026-06-10")
        assert s2 is not None and s2["id"] != s1["id"]
        # Idempotent re-stamp returns the same row
        s2b = db.stamp_pulse_state_for_date("2026-06-10")
        assert s2b["id"] == s2["id"], "re-stamp must be idempotent"
        prev = db.get_prev_stamped_pulse_state("2026-06-10")
        assert prev and prev["pulse_date"] == "2026-06-09"

        # Leans: insert new → re-affirm → age out
        db.upsert_pulse_leans("2026-06-08", [
            {"instrument": "BNO", "direction": "long", "context": "oil floor"},
        ])
        db.upsert_pulse_leans("2026-06-10", [
            {"instrument": "SOXX", "direction": "long", "context": "broadening"},
            {"instrument": "BNO", "direction": "long", "context": "still bid"},
        ])
        board = db.get_board_leans("2026-06-10")
        by_inst = {r["instrument"]: r for r in board}
        assert by_inst["BNO"]["first_seen_date"] == "2026-06-08", (
            "re-affirmed lean keeps its original first_seen"
        )
        assert by_inst["BNO"]["last_seen_date"] == "2026-06-10"
        assert by_inst["SOXX"]["first_seen_date"] == "2026-06-10"
        # Age-out: a lean last seen 6 days before 'today' drops
        db.upsert_pulse_leans("2026-06-04", [
            {"instrument": "GLD", "direction": "short", "context": "old"},
        ])
        board2 = db.get_board_leans("2026-06-10", max_age_days=5)
        assert not any(r["instrument"] == "GLD" for r in board2), (
            "stale lean must age out of the board"
        )
    finally:
        try:
            if db._conn is not None:
                db._conn.close()
        except Exception:
            pass
        db._conn = None
        try:
            os.unlink(tmp.name)
        except PermissionError:
            pass
    _ok("DB round-trip: state stamp idempotency + prev lookup + lean "
        "re-affirm + age-out")


# =====================================================================
# Formatter + fragment hooks
# =====================================================================

def test_formatter_colors_new_sections():
    from report.formatter import _get_section_color
    assert _get_section_color("WHAT CHANGED") == 0x1ABC9C
    assert _get_section_color("TRADE BOARD") == 0x9B59B6
    # Existing sections untouched
    assert _get_section_color("1. RECAP") == 0xFFD700
    assert _get_section_color("3. WHAT TO WATCH") == 0xFF8C00
    _ok("formatter: teal WHAT CHANGED + purple TRADE BOARD, existing "
        "colors intact")


def test_fragment_classifies_new_sections():
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
    import pulse_dashboard
    html = pulse_dashboard._wrap_sections(
        "<h2>WHAT CHANGED</h2><p>x</p>"
        "<h2>TRADE BOARD</h2><p>y</p>"
        "<h2>3. WHAT TO WATCH</h2><p>z</p>"
    )
    assert '<h2 class="changed">' in html
    assert '<h2 class="board">' in html
    assert '<h2 class="watch">' in html, (
        "WHAT TO WATCH must still classify as 'watch' (no collision "
        "with WHAT CHANGED)"
    )
    assert '<div class="changed-body">' in html
    assert '<div class="board-body">' in html
    _ok("fragment: changed/board class hooks, no watch collision")


if __name__ == "__main__":
    print("=== format-overhaul Phase 1 smoke ===")
    test_extract_state_from_ctx()
    test_compute_what_changed_categories()
    test_what_changed_baseline_day_empty()
    test_extract_leans_closing_paragraph_only()
    test_puts_flip_direction()
    test_render_trade_board_new_vs_live()
    test_inject_sections_placement_and_idempotency()
    test_replace_body_after_frontmatter()
    test_db_state_and_leans_roundtrip()
    test_formatter_colors_new_sections()
    test_fragment_classifies_new_sections()
    print("\nALL FORMAT-OVERHAUL PHASE 1 SMOKE TESTS PASS")
