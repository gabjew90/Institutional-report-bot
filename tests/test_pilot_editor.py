"""Shadow editor machinery (pilot piece 5, 2026-09-01): pack order and
ids, citation verification, metric-4 quintiles, leans, finalize salvage."""
import json
import sys

from scripts.pilot_editor_pack import build_pack, order_cards
from scripts.pilot_finalize_edit import extract_markdown, structural_problems
from scripts.pilot_ledger import build
from scripts.pilot_verify_citations import strip_markers, verify


def _card(bank, claim, anchor, instruments=("NVDA",), direction="bullish",
          file="a.json", tier="top", status="target"):
    return {"bank": bank, "document": "doc", "claim": claim, "anchor": anchor,
            "status": status, "instruments": list(instruments), "direction": direction,
            "conviction": "medium", "timeframe": "", "_file": file, "_reader_tier": tier}


CARDS = [
    _card("Goldman", "NVDA target raised to $250", "raised our NVDA target to $250", file="gs.json"),
    _card("Citi", "NVDA stalls at $200", "we see NVDA stalling near $200", direction="bearish", file="citi.json"),
    _card("UBS", "core PCE runs at 3.1%", "core PCE printed 3.1%", instruments=(), file="ubs.json", tier="rest", status="released"),
    _card("Goldman", "AMD lags on 12% share loss", "AMD loses 12% share", instruments=("AMD",), direction="bearish", file="gs.json"),
]
BRIEFS = {"gs.json": {"bank": "Goldman", "title": "Semis", "tier": "top", "brief": "A therefore B."},
          "citi.json": {"bank": "Citi", "title": "NVDA", "tier": "top", "brief": "B therefore C."},
          "ubs.json": {"bank": "UBS", "title": "Macro", "tier": "rest", "brief": "PCE hot."}}


def test_pack_order_is_deterministic_and_groups_by_instrument():
    ordered = order_cards(CARDS, build(CARDS))
    syms = [tuple(c["instruments"]) for c in ordered]
    assert syms[0] == ("NVDA",) and syms[1] == ("NVDA",), syms
    assert syms[-2:] in ([("AMD",), ()], [(), ("AMD",)])
    assert order_cards(CARDS, build(CARDS)) == ordered


def test_pack_assigns_ids_and_lists_every_card_and_brief():
    md, meta = build_pack(CARDS, BRIEFS)
    assert meta["card_count"] == 4 and meta["document_count"] == 3
    assert all(f"[c{i}]" in md for i in range(1, 5))
    assert "[d1]" in md and "Briefs" in md
    assert "Groups are a backstop warning" in md


def _pack():
    _, meta = build_pack(CARDS, BRIEFS)
    return meta


def _cid(meta, claim_fragment):
    return next(k for k, v in meta["cards"].items() if claim_fragment in v["claim"])


def test_hard_check_passes_when_figure_and_bank_match_the_card():
    meta = _pack()
    c = _cid(meta, "target raised")
    md = f"# Head\n\n## 2. THE MAIN EVENT\n\n### T\n\nGoldman lifted its NVDA target to $250 [{c}] [d1].\n\n## 3. BRIEFS\n\n## _LEANS\n\n- long | $NVDA | x\n"
    res = verify(md, meta)
    assert res["failures"] == [], res["failures"]
    assert res["card_citations"] == 1 and res["brief_citations"] == 1
    assert res["leans_block_present"]


def test_hard_check_fails_a_figure_the_card_does_not_carry():
    meta = _pack()
    c = _cid(meta, "target raised")
    md = f"# H\n\n## 2. THE MAIN EVENT\n\n### T\n\nGoldman lifted its NVDA target to $275 [{c}].\n\n## 3. BRIEFS\n\n## _LEANS\n\n- long | $NVDA | x\n"
    res = verify(md, meta)
    assert res["failures"] and "275" in res["failures"][0]["reason"]


def test_hard_check_fails_a_misattributed_bank():
    meta = _pack()
    c = _cid(meta, "target raised")
    md = f"# H\n\n## 2. THE MAIN EVENT\n\n### T\n\nCiti lifted its NVDA target to $250 [{c}].\n\n## 3. BRIEFS\n\n## _LEANS\n\n- long | $NVDA | x\n"
    res = verify(md, meta)
    assert any("bank named" in f["reason"] for f in res["failures"]), res["failures"]


def test_missing_card_or_brief_is_a_failure():
    meta = _pack()
    md = "# H\n\n## 2. THE MAIN EVENT\n\n### T\n\nSomething [c99] and [d42].\n\n## 3. BRIEFS\n\n## _LEANS\n\n- long | $NVDA | x\n"
    res = verify(md, meta)
    reasons = {f["reason"] for f in res["failures"]}
    assert "card does not exist" in reasons and "brief does not exist" in reasons


def test_metric4_edge_share_flags_first_and_last_quintile_heavy_citing():
    cards = [_card("B", f"claim {i} is {i * 10}bp", f"claim {i} is {i * 10}bp", file=f"{i}.json") for i in range(1, 21)]
    _, meta = build_pack(cards, {})
    md = ("# H\n\n## 2. THE MAIN EVENT\n\n### T\n\nSee [c1] and [c2] and [c20] and [c19].\n\n"
          "## 3. BRIEFS\n\n## _LEANS\n\n- long | x | y\n")
    res = verify(md, meta)
    assert res["quintiles"][0] == 2 and res["quintiles"][4] == 2
    assert res["metric4_flag"] is True
    md2 = "# H\n\n## 2. THE MAIN EVENT\n\n### T\n\nSee [c9] and [c10] and [c11] and [c1].\n\n## 3. BRIEFS\n\n## _LEANS\n\n- long | x | y\n"
    assert verify(md2, meta)["metric4_flag"] is False


def test_missing_leans_block_is_structural():
    meta = _pack()
    md = "# H\n\n## 2. THE MAIN EVENT\n\n### T\n\nText.\n\n## 3. BRIEFS\n"
    assert verify(md, meta)["leans_block_present"] is False
    assert "missing ## _LEANS" in structural_problems(md)


def test_a_figure_with_no_card_citation_is_a_failure():
    """Editor rule 1 was unenforced: a sentence with figures and no [cN]
    was never looked at (review 2026-09-01)."""
    meta = _pack()
    md = "# H\n\n## 2. THE MAIN EVENT\n\n### T\n\nCapex goes to $751B this year [d1].\n\n## 3. BRIEFS\n\n## _LEANS\n\n- long | $NVDA | x\n"
    res = verify(md, meta)
    assert any("no card citation" in f["reason"] for f in res["failures"]), res["failures"]


def test_checks_run_once_per_sentence_not_per_citation():
    meta = _pack()
    c1, c2 = _cid(meta, "target raised"), _cid(meta, "stalls")
    md = f"# H\n\n## 2. THE MAIN EVENT\n\n### T\n\nGoldman and Citi split on NVDA at $999 [{c1}] [{c2}].\n\n## 3. BRIEFS\n\n## _LEANS\n\n- long | $NVDA | x\n"
    res = verify(md, meta)
    figure_failures = [f for f in res["failures"] if "figures not in" in f["reason"]]
    assert len(figure_failures) == 1, res["failures"]


def test_bank_alias_matches_the_cited_card():
    """'JPM' in prose against a card whose bank is 'J.P. Morgan'; and
    'Morgan Stanley' must NOT be satisfied by a JPMorgan card."""
    cards = [_card("J.P. Morgan", "pay 5s30s at 62bp", "pay 5s30s at 62bp", instruments=(), file="jpm.json")]
    _, meta = build_pack(cards, {})
    ok = "# H\n\n## 2. THE MAIN EVENT\n\n### T\n\nJPM pays 5s30s at 62bp [c1].\n\n## 3. BRIEFS\n\n## _LEANS\n\n- long | x | y\n"
    assert verify(ok, meta)["failures"] == []
    bad = "# H\n\n## 2. THE MAIN EVENT\n\n### T\n\nMorgan Stanley pays 5s30s at 62bp [c1].\n\n## 3. BRIEFS\n\n## _LEANS\n\n- long | x | y\n"
    assert any("bank named" in f["reason"] for f in verify(bad, meta)["failures"])


def test_index_names_are_not_figures():
    """Shakedown 2026-09-02: 'long Nasdaq 100 against short Russell 2000'
    failed on '100' and '2000', which are names, not figures."""
    cards = [_card("J.P. Morgan", "long Nasdaq against short Russell", "long the Nasdaq and short the Russell", instruments=(), file="j.json")]
    _, meta = build_pack(cards, {})
    md = "# H\n\n## 2. THE MAIN EVENT\n\n### T\n\nJPM opens with long Nasdaq 100 against short Russell 2000 [c1].\n\n## 3. BRIEFS\n\n## _LEANS\n\n- long | x | y\n"
    assert verify(md, meta)["failures"] == []


def test_strip_markers_leaves_clean_prose():
    assert strip_markers("Capex to $751B [c142] rose [d3].") == "Capex to $751B rose."


def test_finalize_salvages_fences_and_preamble():
    raw = "Here is the pulse:\n```markdown\n# Head\n\n## 2. THE MAIN EVENT\n\n### T\n\nx\n\n## 3. BRIEFS\n\n## _LEANS\n\n- long | x | y\n```\nDone."
    md = extract_markdown(raw)
    assert md.startswith("# Head") and md.rstrip().endswith("- long | x | y")
    assert structural_problems(md) == []
    assert extract_markdown("no document here") is None



# 2026-09-03: the editor wrote the day's shadow pulse from 09-02's cards
# while 25 fresh source files sat unread, and the graders scored the
# stale result (m2 fidelity 97% -> 70%) as if it measured the writer.
def test_editor_refuses_to_pack_a_date_with_no_cards_of_its_own(tmpdir=None):
    import json as _json
    import os
    import subprocess
    import sys as _sys
    import tempfile
    from scripts.pilot_editor_pack import has_own_day_cards

    root = tempfile.mkdtemp()
    day2 = os.path.join(root, "2026-09-02")
    os.makedirs(day2)
    with open(os.path.join(day2, "gs.json"), "w", encoding="utf-8") as fh:
        _json.dump({"reader_tier": "top", "cards": [CARDS[0]]}, fh)

    # yesterday has cards, today has none
    assert has_own_day_cards(root, "2026-09-02") is True
    assert has_own_day_cards(root, "2026-09-03") is False
    # an empty directory is not cards either
    os.makedirs(os.path.join(root, "2026-09-04"))
    assert has_own_day_cards(root, "2026-09-04") is False

    # the CLI refuses rather than falling back through the --days window
    out = os.path.join(root, "pack")
    r = subprocess.run(
        [_sys.executable, "scripts/pilot_editor_pack.py", root,
         "--date", "2026-09-03", "--days", "1", "--out", out],
        capture_output=True, text=True)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "REFUSING" in r.stderr and "2026-09-03" in r.stderr
    assert not os.path.exists(out + ".md"), "a refused run must write no pack"
    # backfill can still opt in explicitly
    r2 = subprocess.run(
        [_sys.executable, "scripts/pilot_editor_pack.py", root,
         "--date", "2026-09-03", "--days", "1", "--out", out, "--allow-stale"],
        capture_output=True, text=True)
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert os.path.exists(out + ".md")

if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")
