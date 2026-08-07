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


def test_render_trade_board_new_vs_live():
    from report.pulse_sections import render_trade_board
    # 2026-06-23: context_snippet now holds the FULL board display line
    # (built upstream by parse_lean_block / extract_leans_from_markdown);
    # render just prefixes the status.
    rows = [
        {"instrument": "SOXX", "direction": "long",
         "first_seen_date": "2026-06-10", "last_seen_date": "2026-06-10",
         "context_snippet": "Long $SOXX — broadening thesis"},
        {"instrument": "BNO", "direction": "long",
         "first_seen_date": "2026-06-08", "last_seen_date": "2026-06-10",
         "context_snippet": "Long $BNO — oil floor vs futures fade"},
    ]
    board = render_trade_board(rows, "2026-06-10")
    assert "## TRADE BOARD" in board
    # De-monospaced 2026-06-19: clean markdown bullets, NOT a ``` block.
    assert "```" not in board, "board must NOT be a monospace code block"
    # 2026-06-22: NEW for first-today, "held since <date>" for carries
    # (no cryptic/weekend-inflated dN).
    assert "- **NEW**" in board and "$SOXX" in board
    assert "- **held since Jun 8**" in board and "$BNO" in board, board
    assert render_trade_board([], "2026-06-10") == ""
    _ok("trade board: NEW vs 'held since <date>', clean bullets (no "
        "monospace/dN), empty when no rows")


def test_board_shows_only_todays_calls():
    """2026-06-22 QC: the board's LIVE section shows only leans
    re-affirmed today. 2026-07-10 amendment: a lean from the
    immediately-prior board that today's pulse didn't repeat now renders
    as an explicit **DROPPED** line instead of vanishing silently (four
    of five leans disappeared without a trace while the prose reversed
    the RSP thesis)."""
    from report.pulse_sections import render_trade_board
    rows = [
        # today's call — shown live
        {"instrument": "TLT", "direction": "short",
         "first_seen_date": "2026-06-17", "last_seen_date": "2026-06-22",
         "context_snippet": "Short $TLT — into PCE"},
        # prior-board carry the pulse didn't repeat — shown as DROPPED
        {"instrument": "VIXY", "direction": "long",
         "first_seen_date": "2026-06-18", "last_seen_date": "2026-06-19",
         "context_snippet": "Long $VIXY — into next week's data"},
    ]
    board = render_trade_board(rows, "2026-06-22",
                               prev_board_date="2026-06-19")
    assert "$TLT" in board, "today's re-affirmed lean must show"
    assert "held since Jun 17" in board, board
    assert "- **off board since Jun 19** Long $VIXY" in board, \
        f"prior-board lean must surface as off-board, not vanish: {board}"
    assert board.count("$VIXY") == 1, "dropped lean renders exactly once"
    # the bridge must feed the real prior-pulse date (daily_reports),
    # not leave the param unset
    import inspect as _i
    import github_bridge.jobs as _gbj
    assert "get_prev_scheduled_pulse_date" in _i.getsource(_gbj), \
        "bridge must pass prev_board_date from daily_reports"
    _ok("board: today's calls live; prior-board leans surface as DROPPED")


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


def test_board_self_ref_and_options():
    """2026-06-19 fixes, now in the display builders: (1) self-ref clause
    stripping (_clean_board_context, prose-fallback path); (2) options
    shown without a Long/Short prefix (_build_lean_display)."""
    from report.pulse_sections import _clean_board_context, _build_lean_display
    # Self-ref: $UUP's own clause stripped from its descriptor (fallback).
    pair = "Short $TLT, paired with long $UUP, into PCE, sized for liquidity."
    uup = _clean_board_context(pair, "UUP", "long")
    assert "$UUP" not in uup, f"self-reference not stripped: {uup!r}"
    assert uup.lower().startswith("into pce"), uup
    # The OTHER leg keeps the (legit) cross-reference.
    tlt = _clean_board_context(pair, "TLT", "short")
    assert "$UUP" in tlt, "the TLT row should still name its UUP pair leg"
    # Options carry direction in the contract — no Long/Short prefix.
    puts = _build_lean_display("short", "$SMH puts", "cheap insurance")
    assert puts.startswith("$SMH puts"), puts
    assert "Short" not in puts, f"options must not get a Short prefix: {puts!r}"
    calls = _build_lean_display("long", "$BNO calls", "cheap upside")
    assert calls.startswith("$BNO calls") and "Long" not in calls, calls
    # Non-options DO get the direction prefix.
    tltd = _build_lean_display("short", "$TLT", "2-year overdone")
    assert tltd.startswith("Short $TLT · 2-year overdone"), tltd
    _ok("display: self-ref stripped (fallback) + options have no Long/Short "
        "prefix + plain leans get direction prefix")


def test_hc_calls_subsection():
    """2026-06-24: HIGH-CONVICTION single-name calls brought back from the
    retired DESK SIGNAL BOARD as a clean subsection under the TRADE BOARD
    — markdown bullets (no monospace), bank bolded + ticker cashtagged,
    foreign-PT calls dropped, N/A rating skipped, capped."""
    from report.pulse_sections import render_trade_board, _HC_SUBSECTION_MAX
    hc = [
        {"source": "Goldman Sachs", "ticker": "ASML", "rating": "Buy",
         "pt": "", "rationale": "fundamental story strong, EUV demand durable"},
        {"source": "JPMorgan", "ticker": "JBL", "rating": "", "action": "",
         "pt": "$450", "rationale": "accelerating FY27 AI revenue"},
        {"source": "Barclays", "ticker": "BESI", "rating": "Buy",
         "pt": "EUR315", "rationale": "foreign-listed, must be dropped"},
        {"source": "The Market Ear", "ticker": "CRM", "rating": "N/A",
         "pt": "N/A", "rationale": "record losing streak"},
    ]
    lean_rows = [{"instrument": "TLT", "direction": "short",
                  "first_seen_date": "2026-06-24", "last_seen_date": "2026-06-24",
                  "context_snippet": "Short $TLT — into PCE"}]
    board = render_trade_board(lean_rows, "2026-06-24", None, hc)
    # one ## TRADE BOARD header, leans + HC under it
    assert board.count("## TRADE BOARD") == 1, board
    assert "High-conviction single-name calls" in board
    assert "```" not in board, "HC subsection must NOT be monospace"
    # clean rendering: bank bolded, ticker cashtagged, rating/PT
    assert "**Goldman Sachs** $ASML · Buy" in board, board
    assert "**JPMorgan** $JBL · PT $450" in board, board
    # foreign-PT call dropped, N/A rating not shown as a token
    assert "$BESI" not in board, "foreign-PT (EUR) call must be dropped"
    assert "N/A" not in board, "N/A rating/PT must not render"
    # 2026-07-06: NT$ (New Taiwan dollar symbol) is a foreign PT — a
    # $TSM call priced "NT$3,000" must drop (TWD ISO was covered, the
    # NT$ symbol form wasn't).
    from report.pulse_sections import _is_foreign_pt
    assert _is_foreign_pt("NT$3,000") and _is_foreign_pt("S$45") \
        and _is_foreign_pt("NZ$12"), "Asian/NZ dollar symbols must flag"
    assert not _is_foreign_pt("$330") and not _is_foreign_pt("$1,400"), \
        "plain USD price targets must NOT be flagged foreign"
    # 2026-06-25: a full-sentence rationale renders WHOLE, not clipped at
    # 60 mid-word ("disclosed $100bn…").
    long_hc = [{"source": "Goldman Sachs", "ticker": "MU", "rating": "Buy",
                "pt": "",
                "rationale": "smashed expectations, raised the outlook, and "
                "disclosed $100bn of contracted HBM revenue through 2027"}]
    lb = render_trade_board([], "2026-06-25", None, long_hc)
    assert "contracted HBM revenue through 2027" in lb, (
        f"full HC rationale must render, not truncate mid-thought: {lb}"
    )
    assert "…" not in lb, "a sub-170-char rationale must not get an ellipsis"
    # HC-only (no leans) still renders the board
    hc_only = render_trade_board([], "2026-06-24", None, hc)
    assert "## TRADE BOARD" in hc_only and "$ASML" in hc_only
    # 2026-06-30: a call with a BLANK/"?" ticker (the extractor now blanks
    # foreign / collision-risk names like BAE Systems) is dropped — no
    # cashtag, no place on a US-actionable board.
    blanked = [{"source": "Morgan Stanley", "ticker": "", "rating": "OW",
                "pt": "", "rationale": "BAE Systems new Top Pick in EU Defense"},
               {"source": "UBS", "ticker": "FSLR", "rating": "Buy",
                "pt": "$330", "rationale": "Section 232 tariff upside"}]
    bd = render_trade_board([], "2026-06-30", None, blanked)
    assert "BAE Systems" not in bd, "tickerless (blanked-foreign) call must drop"
    assert "$FSLR" in bd, "the real US-tickered call must still render"
    # neither leans nor HC -> empty
    assert render_trade_board([], "2026-06-24", None, []) == ""
    _ok("board: HC subsection clean bullets; foreign/N-A/tickerless dropped, "
        "renders with or without leans")


def test_board_caps_rows():
    from report.pulse_sections import render_trade_board, _BOARD_MAX_ROWS
    rows = [
        {"instrument": f"TIC{i}", "direction": "long",
         "first_seen_date": "2026-06-01", "last_seen_date": "2026-06-19",
         "context_snippet": "x"}
        for i in range(_BOARD_MAX_ROWS + 6)
    ]
    board = render_trade_board(rows, "2026-06-19")
    bullet_count = sum(1 for l in board.splitlines() if l.startswith("- **"))
    assert bullet_count == _BOARD_MAX_ROWS, (
        f"board must cap at {_BOARD_MAX_ROWS}, got {bullet_count}"
    )
    _ok(f"board: caps at {_BOARD_MAX_ROWS} rows (no wall of stale carries)")


def test_board_flip_marker():
    from report.pulse_sections import render_trade_board
    rows = [
        {"instrument": "USO", "direction": "short",
         "first_seen_date": "2026-06-15", "last_seen_date": "2026-06-15",
         "context_snippet": "Short $USO, with an energy underweight."},
    ]
    # Check the bullet rows, not the legend line (which always names
    # FLIP/NEW). The row marker is bolded — "- **NEW**" / "- **FLIP**".
    plain = render_trade_board(rows, "2026-06-15")
    plain_rows = "\n".join(
        l for l in plain.splitlines() if l.startswith("- **")
    )
    assert "**NEW**" in plain_rows and "**FLIP**" not in plain_rows, plain_rows
    # With USO in the flip set → FLIP marker instead of NEW
    flipped = render_trade_board(rows, "2026-06-15", {"USO"})
    flipped_rows = "\n".join(
        l for l in flipped.splitlines() if l.startswith("- **")
    )
    assert "**FLIP**" in flipped_rows and "$USO" in flipped_rows, flipped_rows
    assert "**NEW**" not in flipped_rows, flipped_rows
    _ok("board: FLIP marker shown for reversed-today instruments")


# =====================================================================
# Injection
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


def test_inject_sections_placement_and_idempotency():
    from report.pulse_sections import inject_sections
    # 2026-06-19: inject_sections is board-only now (WHAT CHANGED + DESK
    # SIGNAL BOARD were cut). The board anchors before WHAT TO WATCH.
    board = "## TRADE BOARD\n\n- **NEW** Long $SOXX\n"
    out = inject_sections(_PULSE_MD, board)
    # TRADE BOARD between INSIGHTS and WATCH
    assert out.index("## 2. INSIGHTS") < out.index("## TRADE BOARD") < \
           out.index("## 3. WHAT TO WATCH"), "BOARD must sit before WATCH"
    # No WHAT CHANGED / DESK SIGNAL injected anymore.
    assert "## WHAT CHANGED" not in out and "## DESK SIGNAL" not in out
    # Idempotency — re-injection is a no-op
    again = inject_sections(out, board)
    assert again == out, "re-injection must not duplicate the board"
    _ok("injection: board-only placement before WATCH + idempotent")


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
    test_render_trade_board_new_vs_live()
    test_board_shows_only_todays_calls()
    test_board_self_ref_and_options()
    test_hc_calls_subsection()
    test_board_caps_rows()
    test_clean_board_context_no_garble()
    test_board_flip_marker()
    test_inject_sections_placement_and_idempotency()
    test_replace_body_after_frontmatter()
    test_db_state_and_leans_roundtrip()
    test_formatter_colors_new_sections()
    test_fragment_classifies_new_sections()
    print("\nALL FORMAT-OVERHAUL PHASE 1 SMOKE TESTS PASS")
