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
    assert "New theme" in joined and "boj policy pivot" in joined, bullets
    assert "Faded" in joined and "old fading theme" in joined, bullets
    # HC calls are NOT listed in WHAT CHANGED anymore — they're owned by
    # the DESK SIGNAL BOARD (2026-06-17 de-dup). Neither the fresh call
    # ($MU) nor the carried one (NVDA) should appear here.
    assert "Fresh high-conviction" not in joined, bullets
    assert "$MU" not in joined and "NVDA" not in joined, bullets
    _ok("compute_what_changed: flip + new + faded detected; HC calls "
        "NOT duplicated here (owned by DESK SIGNAL BOARD)")


def test_what_changed_baseline_day_empty():
    from report.pulse_sections import compute_what_changed, render_what_changed
    assert compute_what_changed(None, {"themes": [], "hc_calls": []}) == []
    assert render_what_changed([]) == "", (
        "baseline day renders NOTHING — no empty-section noise"
    )
    _ok("baseline day: no bullets, no section")


def test_what_changed_lean_flip_leads_no_hc():
    """Lean flips surface as the top bullet. HC calls do NOT appear in
    WHAT CHANGED at all (2026-06-17: they moved to the DESK SIGNAL
    BOARD; listing them in both duplicated every call)."""
    from report.pulse_sections import compute_what_changed
    prev = {"themes": [{"label": "oil scare", "banks": 6, "sup": 4, "skep": 0, "hc": 0}],
            "hc_calls": []}
    today = {"themes": [{"label": "oil scare", "banks": 6, "sup": 4, "skep": 0, "hc": 0}],
             "hc_calls": [
                 {"source": "Goldman Sachs", "ticker": "MU",
                  "action": "reiterate Buy", "pt": "$900"},
                 {"source": "Goldman Sachs", "ticker": "GALP",
                  "action": "reiterate", "pt": "EUR24"},
             ]}
    bullets = compute_what_changed(
        prev, today,
        lean_flips=[{"instrument": "USO", "from": "long", "to": "short"}],
        body_tickers={"MU", "USO", "TLT"},
    )
    joined = " | ".join(bullets)
    assert bullets[0].startswith("**Flipped:**") and "$USO" in bullets[0], bullets
    assert "long → short" in bullets[0]
    # No HC calls leak into WHAT CHANGED — neither body-present ($MU)
    # nor body-absent ($GALP). The board owns the calls roster.
    assert "$MU" not in joined and "GALP" not in joined, bullets
    assert "Fresh high-conviction" not in joined, bullets
    _ok("WHAT CHANGED: lean flip leads; HC calls not duplicated here")


def test_what_changed_suppresses_clusterer_renames():
    """06-18: the clusterer relabels the same topic day-over-day, firing
    spurious New+Faded pairs ('us iran geopolitical relief' ->
    'iran nuclear deal viability', 'Fed Chair Warsh press conference' ->
    'termination of forward guidance by Fed Chair Kevin Warsh'). Renames
    must not show as new OR faded."""
    from report.pulse_sections import compute_what_changed
    prev = {"themes": [
        {"label": "Fed Chair Kevin Warsh's first press conference",
         "banks": 8, "sup": 0, "skep": 0, "hc": 0},
        {"label": "us iran geopolitical relief", "banks": 9,
         "sup": 3, "skep": 0, "hc": 0},
    ], "hc_calls": []}
    today = {"themes": [
        {"label": "Termination of forward guidance by Fed Chair Kevin Warsh",
         "banks": 12, "sup": 0, "skep": 0, "hc": 0},
        {"label": "iran nuclear deal viability", "banks": 10,
         "sup": 0, "skep": 1, "hc": 0},
    ], "hc_calls": []}
    bullets = compute_what_changed(prev, today)
    joined = " | ".join(bullets)
    # Strong rename (3 shared: fed/chair/kevin) suppressed from "new"
    assert "Termination of forward guidance" not in joined, bullets
    # No faded churn for either renamed topic
    assert "Faded" not in joined, bullets
    _ok("WHAT CHANGED: clusterer renames don't fire New (strong) or "
        "Faded (any-overlap) churn")


def test_what_changed_lead_theme_change():
    from report.pulse_sections import compute_what_changed
    prev = {"themes": [{"label": "oil scare", "banks": 7, "sup": 1, "skep": 5, "hc": 0},
                       {"label": "ai capex", "banks": 6, "sup": 4, "skep": 0, "hc": 0}],
            "hc_calls": []}
    today = {"themes": [{"label": "ai capex", "banks": 6, "sup": 4, "skep": 0, "hc": 0},
                        {"label": "oil scare", "banks": 4, "sup": 1, "skep": 2, "hc": 0}],
             "hc_calls": []}
    bullets = compute_what_changed(prev, today)
    joined = " | ".join(bullets)
    assert "Lead theme" in joined and "ai capex" in joined, bullets
    _ok("WHAT CHANGED: lead-theme rotation surfaced")


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


def test_clean_board_context_no_garble():
    """Regression for the 06-15 garble: board context must be a clean
    descriptor, never a mid-word truncation or a broken-grammar
    fragment ('For US-listed exposure, is the bet…', '$MU  The lean is
    , Morgan Stanley Overweight)')."""
    from report.pulse_sections import _clean_board_context, _BOARD_CTX_LIMIT
    # Leading lean → stripped to a clean continuation
    c1 = _clean_board_context(
        "Short $USO, with an energy-sector underweight into the de-escalation.",
        "USO", "short")
    assert c1.startswith("With an energy-sector underweight"), c1
    assert "$USO" not in c1 and "Short" not in c1
    # Mid-sentence lean (sentence opens with non-verb) → kept whole,
    # never the broken "is the bet…" opener
    c2 = _clean_board_context(
        "For US-listed exposure, long the currency-hedged $DXJ is the bet "
        "the yen firms and the hedge pays, against unhedged $EWJ.",
        "DXJ", "long")
    assert c2.startswith("For US-listed exposure"), c2
    # Word-boundary clip + ellipsis, never mid-word, within limit+1
    assert "…" in c2
    assert " " not in c2[-3:], f"clipped mid-word: {c2!r}"
    assert len(c2) <= _BOARD_CTX_LIMIT + 1
    # Lean-opener that left a dangling copula → 'is/the' junk dropped
    c3 = _clean_board_context(
        "Long $TLT is the bet he doesn't and the cuts eventually come back.",
        "TLT", "long")
    assert not c3.lower().startswith("is "), c3
    assert "$TLT" not in c3
    # Empty / unusable context → empty string (board shows bare line)
    assert _clean_board_context("", "X", "long") == ""
    _ok("board context: leading-lean strip, mid-sentence keep, "
        "word-boundary clip+ellipsis, junk-opener drop")


def test_board_flip_marker():
    from report.pulse_sections import render_trade_board
    rows = [
        {"instrument": "USO", "direction": "short",
         "first_seen_date": "2026-06-15", "last_seen_date": "2026-06-15",
         "context_snippet": "Short $USO, with an energy underweight."},
    ]
    # Without flip info → NEW (check the code block, not the legend
    # which always names FLIP)
    plain_block = render_trade_board(rows, "2026-06-15").split("```")[1]
    assert "NEW " in plain_block and "FLIP" not in plain_block, plain_block
    # With USO in the flip set → FLIP marker instead of NEW
    flipped_block = render_trade_board(rows, "2026-06-15", {"USO"}).split("```")[1]
    assert "FLIP" in flipped_block and "$USO" in flipped_block, flipped_block
    assert "NEW " not in flipped_block, flipped_block
    _ok("board: FLIP marker shown for reversed-today instruments")


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

        # Stance flip: a live long, then a short on the same instrument
        # supersedes the long so the board never shows both (the 06-15
        # $USO long+short contradiction).
        db.upsert_pulse_leans("2026-06-09", [
            {"instrument": "USO", "direction": "long", "context": "long oil"},
        ])
        flips = db.upsert_pulse_leans("2026-06-10", [
            {"instrument": "USO", "direction": "short", "context": "Short $USO, de-escalation."},
        ])
        assert flips == [{"instrument": "USO", "from": "long", "to": "short"}], flips
        board3 = db.get_board_leans("2026-06-10")
        uso_rows = [r for r in board3 if r["instrument"] == "USO"]
        assert len(uso_rows) == 1, f"only one USO row may be live: {uso_rows}"
        assert uso_rows[0]["direction"] == "short", uso_rows
        # Re-affirm on a retry → no duplicate flip
        flips_again = db.upsert_pulse_leans("2026-06-10", [
            {"instrument": "USO", "direction": "short", "context": "Short $USO."},
        ])
        assert flips_again == [], f"retry must not re-flip: {flips_again}"
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
    test_what_changed_lean_flip_leads_no_hc()
    test_what_changed_suppresses_clusterer_renames()
    test_what_changed_lead_theme_change()
    test_extract_leans_closing_paragraph_only()
    test_puts_flip_direction()
    test_render_trade_board_new_vs_live()
    test_clean_board_context_no_garble()
    test_board_flip_marker()
    test_inject_sections_placement_and_idempotency()
    test_replace_body_after_frontmatter()
    test_db_state_and_leans_roundtrip()
    test_formatter_colors_new_sections()
    test_fragment_classifies_new_sections()
    print("\nALL FORMAT-OVERHAUL PHASE 1 SMOKE TESTS PASS")
