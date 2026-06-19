"""Smoke test: DESK SIGNAL BOARD (format-overhaul Phase 2, §3).

A deterministic section assembled by code from the stamped pulse state
(no LLM, no fabrication surface), two sub-blocks:
  HIGH-CONVICTION CALLS — source, ticker, rating/action, PT, rationale
  CONSENSUS LEDGER — multi-bank themes: bull/bear split, banks, HC
    count, and NAMED dissent.

Covers:
  - synthesizer exposes skeptical_sources in theme_map (dissent names)
  - extract_state_from_ctx captures HC rating+rationale + theme
    skep_sources
  - render_desk_signal_board: HC table, consensus ledger, dissent
    naming, no-dissent line, banks>=2 filter, HC cap, empty-when-empty
  - inject order: WHAT CHANGED -> DESK SIGNAL -> INSIGHTS; idempotent
  - formatter color + dashboard class hook (no collision with TRADE
    BOARD's 'board' class)
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_extract_captures_hc_detail_and_dissent():
    from report.pulse_sections import extract_state_from_ctx
    ctx = {
        "theme_map": {
            "ai positioning": {
                "banks": 6, "supportive": 3, "skeptical": 1,
                "high_conviction": 2,
                "skeptical_sources": ["Goldman Sachs", "Morgan Stanley", "TME", "X"],
            },
        },
        "analyses_json": json.dumps([
            {"source": "Citi", "market_movers": [
                {"ticker": "NVDA", "conviction": "high", "action": "reiterate",
                 "rating": "Buy", "price_target": "$300",
                 "rationale": "Vera Rubin cycle demand"},
            ]},
        ]),
    }
    state = extract_state_from_ctx(ctx)
    t = state["themes"][0]
    assert t["skep_sources"] == ["Goldman Sachs", "Morgan Stanley", "TME"], (
        f"skeptical_sources captured + capped at 3: {t['skep_sources']}"
    )
    c = state["hc_calls"][0]
    assert c["rating"] == "Buy" and c["rationale"] == "Vera Rubin cycle demand"
    _ok("extract_state: HC rating+rationale + theme skep_sources (cap 3)")


def _state():
    return {
        "hc_calls": [
            {"source": "Citi", "ticker": "NVDA", "action": "reiterate",
             "rating": "Buy", "pt": "$300", "rationale": "Vera Rubin cycle"},
            {"source": "Goldman Sachs", "ticker": "LLY", "action": "reiterate",
             "rating": "Buy", "pt": "$1,500", "rationale": "Phase 3 readout"},
        ],
        "themes": [
            {"label": "ai positioning", "banks": 6, "sup": 3, "skep": 1,
             "hc": 2, "skep_sources": ["Goldman Sachs"]},
            {"label": "boj june hike", "banks": 6, "sup": 6, "skep": 0,
             "hc": 2, "skep_sources": []},
            {"label": "niche single-bank idea", "banks": 1, "sup": 1,
             "skep": 0, "hc": 0, "skep_sources": []},  # banks<2 -> excluded
        ],
    }


def test_render_hc_and_ledger():
    from report.pulse_sections import render_desk_signal_board
    out = render_desk_signal_board(_state())
    assert "## DESK SIGNAL BOARD" in out
    assert "```" in out, "monospace block (Discord embeds don't render tables)"
    # HC calls
    assert "HIGH-CONVICTION CALLS" in out
    assert "$NVDA" in out and "Buy" in out and "PT $300" in out
    assert "Vera Rubin cycle" in out
    assert "$LLY" in out and "PT $1,500" in out
    # Consensus ledger
    assert "CONSENSUS LEDGER" in out
    assert "ai positioning — 3 bull / 1 bear · 6 banks · 2 HC" in out, out
    assert "dissent: Goldman Sachs" in out
    # No-dissent theme
    assert "boj june hike — 6 bull / 0 bear · 6 banks · 2 HC · no dissent" in out, out
    # banks<2 theme excluded
    assert "niche single-bank idea" not in out
    _ok("render: HC table + consensus ledger + named dissent + "
        "no-dissent line + banks>=2 filter")


def test_hc_formatting_fixes():
    """06-17 fixes: foreign-currency calls dropped, ratings normalized
    (no mid-word 'Overweigh'), rationale word-boundary clipped + ellipsis."""
    from report.pulse_sections import render_desk_signal_board
    state = {
        "hc_calls": [
            {"source": "JPMorgan", "ticker": "AVGO", "action": "reiterate",
             "rating": "Overweight", "pt": "$580.00",
             "rationale": "Broadcom maintains a significant 18-month lead in custom AI silicon"},
            {"source": "HSBC", "ticker": "MTN", "action": "reiterate",
             "rating": "Buy", "pt": "ZAR280.0", "rationale": "Nigeria recovery"},
            {"source": "GS", "ticker": "PUIG", "action": "reiterate",
             "rating": "Buy", "pt": "€21.50", "rationale": "Re-rating potential"},
        ],
        "themes": [],
    }
    out = render_desk_signal_board(state)
    # Foreign-currency calls dropped
    assert "$MTN" not in out and "ZAR" not in out, "ZAR call must drop"
    assert "$PUIG" not in out and "€" not in out, "EUR call must drop"
    # US call kept, rating normalized (no mid-word truncation)
    assert "$AVGO" in out
    assert "Overweigh " not in out and "Overweight" not in out, out
    assert "OW" in out, "Overweight should normalize to OW"
    # Rationale word-boundary clipped with ellipsis
    avgo_line = next(l for l in out.splitlines() if "$AVGO" in l)
    assert "…" in avgo_line, avgo_line

    # Word-boundary property tested directly on the helper: the clipped
    # text (minus the ellipsis) must end exactly at a word boundary in
    # the original — never a mid-word stub like "combi"/"includi".
    from report.pulse_sections import _clip_rationale
    long = "Broadcom maintains a significant 18-month lead in custom AI silicon"
    clipped = _clip_rationale(long, 40)
    assert clipped.endswith("…") and len(clipped) <= 41, clipped
    stem = clipped[:-1]
    assert long.startswith(stem), (stem, long)
    # the char in the original right after the kept stem is a space →
    # we cut on a word boundary, not mid-word
    assert long[len(stem)] == " ", f"clipped mid-word: {clipped!r}"
    assert _clip_rationale("short one", 40) == "short one", "no clip when short"
    _ok("HC formatting: foreign dropped, rating normalized, rationale "
        "clipped on word boundary + ellipsis")


def test_ledger_all_neutral_row():
    from report.pulse_sections import render_desk_signal_board
    state = {"hc_calls": [], "themes": [
        {"label": "warsh debut", "banks": 8, "sup": 0, "skep": 0,
         "hc": 0, "skep_sources": []},
    ]}
    out = render_desk_signal_board(state)
    assert "0 bull / 0 bear" not in out, "all-neutral must not read as empty"
    assert "warsh debut — 8 banks tracking · no directional lean" in out, out
    _ok("ledger: all-neutral theme reads 'N banks tracking · no "
        "directional lean'")


def test_render_empty_when_no_data():
    from report.pulse_sections import render_desk_signal_board
    assert render_desk_signal_board(None) == ""
    assert render_desk_signal_board({"hc_calls": [], "themes": []}) == ""
    # only single-bank themes + no HC -> nothing to show
    assert render_desk_signal_board(
        {"hc_calls": [], "themes": [{"label": "x", "banks": 1}]}) == ""
    _ok("render: empty string when no HC calls and no multi-bank themes")


def test_render_hc_cap():
    from report.pulse_sections import render_desk_signal_board, _HC_CALLS_MAX
    many = {"hc_calls": [
        {"source": "B", "ticker": f"T{i}", "rating": "Buy", "pt": "",
         "rationale": ""} for i in range(_HC_CALLS_MAX + 3)
    ], "themes": []}
    out = render_desk_signal_board(many)
    assert f"+3 more" in out, "must note dropped HC calls past the cap"
    _ok("render: HC calls capped with '+N more' note")


def test_desk_signal_no_longer_injected():
    """2026-06-19: DESK SIGNAL BOARD was CUT from the pulse pipeline
    (read as duplicating the prose). inject_sections is board-only now;
    the render fn still exists (dormant) but nothing injects it. This
    guards that the section does NOT come back into the pulse via inject."""
    from report.pulse_sections import inject_sections
    md = (
        "# Pulse\n\n## 1. RECAP\n\nstuff\n\n"
        "## 2. INSIGHTS & ALPHA\n\nthemes\n\n"
        "## 3. WHAT TO WATCH\n\ncalendar\n"
    )
    board = "## TRADE BOARD\n\n- **NEW** Long $X\n"
    out = inject_sections(md, board)
    assert "## DESK SIGNAL BOARD" not in out, "DESK SIGNAL must not be injected"
    assert "## WHAT CHANGED" not in out, "WHAT CHANGED must not be injected"
    assert (out.index("## 2. INSIGHTS") < out.index("## TRADE BOARD")
            < out.index("## 3. WHAT TO WATCH")), out
    again = inject_sections(out, board)
    assert again == out, "re-injection must not duplicate"
    _ok("inject: DESK SIGNAL + WHAT CHANGED no longer injected; board-only")


def test_formatter_color():
    from report.formatter import _get_section_color
    assert _get_section_color("DESK SIGNAL BOARD") == 0x16A085
    # must NOT collide with TRADE BOARD's purple
    assert _get_section_color("TRADE BOARD") == 0x9B59B6
    _ok("formatter: DESK SIGNAL distinct color, no TRADE BOARD collision")


def test_dashboard_classify_no_board_collision():
    import pulse_dashboard
    html = pulse_dashboard._wrap_sections(
        "<h2>DESK SIGNAL BOARD</h2><p>x</p>"
        "<h2>TRADE BOARD</h2><p>y</p>"
    )
    assert '<h2 class="signal">' in html, "DESK SIGNAL BOARD -> signal, not board"
    assert '<h2 class="board">' in html, "TRADE BOARD still -> board"
    assert '<div class="signal-body">' in html
    assert '<div class="board-body">' in html
    _ok("dashboard: 'DESK SIGNAL BOARD' classifies as signal (no collision "
        "with TRADE BOARD board class)")


if __name__ == "__main__":
    print("=== DESK SIGNAL BOARD (Phase 2) smoke ===")
    test_extract_captures_hc_detail_and_dissent()
    test_render_hc_and_ledger()
    test_hc_formatting_fixes()
    test_ledger_all_neutral_row()
    test_render_empty_when_no_data()
    test_render_hc_cap()
    test_desk_signal_no_longer_injected()
    test_formatter_color()
    test_dashboard_classify_no_board_collision()
    print("\nALL DESK SIGNAL BOARD SMOKE TESTS PASS")
