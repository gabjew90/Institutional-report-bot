"""Smoke test: Phase B promotion merges into existing Phase A theme
sources rather than creating a "(discovered)" duplicate.

Background: 2026-06-04 pulse QC flagged that `us_labor_market_strength`
was discarded in adjudication with reason `facts_agreed banks_for not
in input sources: 'Scotiabank'`. The QC review correctly identified
that Scotiabank IS in the corpus — but as contextual mentions, not as
an explicit theme_stance on the labor topic. Phase A's `theme_map[tag]
["sources"]` only included banks with explicit stances, so Scotiabank
wasn't in the input-source list adjudication validates against.

Phase B identified the same labor theme via contextual mentions (with
Scotiabank in d["banks"]), but the old code created a SEPARATE
"us_labor_market_strength (discovered)" entry instead of merging the
bank lists. Adjudication ran on the Phase A entry without seeing
Scotiabank.

Same shape on 2026-06-01 IEA case.

Fix: when Phase B promotion's canonical label matches an existing
Phase A theme, MERGE Phase B's banks into the Phase A entry's sources
list. "(discovered)" suffix is reserved for genuinely-new canonicals.
"""

import sys


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _apply_phase_b_merge(theme_map, promoted):
    """Mirrors the production merge block in synthesizer.py — extracted
    here so tests can exercise the fix in isolation without needing the
    full _classify_themes pipeline (which depends on embeddings)."""
    for d in promoted:
        label = d["canonical"]
        banks_set = set(d["banks"])
        if label in theme_map:
            existing = theme_map[label]
            merged_banks = set(existing["sources"]) | banks_set
            existing["sources"] = sorted(merged_banks)
            existing["banks"] = len(merged_banks)
            existing["pdfs"] = max(
                existing.get("pdfs", 0), len(d["pdf_ids"])
            )
            existing["reinforced_by_discovery"] = True
        else:
            theme_map[label] = {
                "banks": len(banks_set),
                "pdfs": len(d["pdf_ids"]),
                "sources": sorted(banks_set),
                "supportive": 0,
                "skeptical": 0,
                "neutral": len(banks_set),
                "discovered": True,
            }


def test_phase_b_merges_into_phase_a_when_canonical_collides():
    """Simulate the Scotiabank case: Phase A has the labor theme with
    sources [BofA, JPM]. Phase B promotes the same canonical with
    mention-banks [BofA, Scotiabank]. After the merge, sources must
    be [BofA, JPM, Scotiabank] — NOT a separate '(discovered)' entry."""
    theme_map = {
        "us labor market strength": {
            "banks": 2, "pdfs": 2,
            "sources": ["BofA Global Research", "JPMorgan"],
            "supportive": 2, "skeptical": 0, "neutral": 0,
            "discovered": False,
        }
    }
    promoted = [{
        "canonical": "us labor market strength",
        "banks": ["BofA Global Research", "Scotiabank"],
        "pdf_ids": [1, 3],
    }]
    _apply_phase_b_merge(theme_map, promoted)

    sources = theme_map["us labor market strength"]["sources"]
    assert "Scotiabank" in sources, (
        f"Scotiabank missing from sources after merge: {sources}"
    )
    assert "BofA Global Research" in sources and "JPMorgan" in sources
    assert theme_map["us labor market strength"]["banks"] == 3
    assert theme_map["us labor market strength"]["reinforced_by_discovery"] is True
    assert "us labor market strength (discovered)" not in theme_map, (
        "merge should suppress the (discovered) duplicate when the "
        "canonical label matches an existing Phase A theme"
    )
    _ok("Phase B merges into existing Phase A entry (Scotiabank added to sources)")


def test_phase_b_keeps_discovered_suffix_for_new_canonical():
    """Sanity: Phase B's '(discovered)' branch still fires for genuinely
    new canonicals that Phase A didn't see. Guard against regression."""
    theme_map = {
        "ai infrastructure and capex": {
            "banks": 7, "pdfs": 8, "sources": ["BofA", "Citi"],
            "supportive": 5, "skeptical": 2, "neutral": 0,
            "discovered": False,
        }
    }
    promoted = [{
        "canonical": "data center power demand",  # NEW — not in Phase A
        "banks": ["ANZ", "BofA", "RBC", "UBS"],
        "pdf_ids": [10, 11, 12, 13],
    }]
    _apply_phase_b_merge(theme_map, promoted)

    assert "data center power demand" in theme_map
    assert theme_map["data center power demand"]["discovered"] is True
    assert theme_map["data center power demand"]["banks"] == 4
    _ok("Phase B keeps discovered=True for genuinely-new canonicals")


def test_merge_does_not_double_count_pdfs():
    """When a PDF contributes both a Phase A stance and a Phase B mention
    for the same canonical, pdf count uses max() rather than sum()."""
    theme_map = {
        "x": {"banks": 2, "pdfs": 5, "sources": ["A", "B"],
              "supportive": 2, "skeptical": 0, "neutral": 0,
              "discovered": False},
    }
    promoted = [{
        "canonical": "x",
        "banks": ["B", "C"],
        "pdf_ids": [1, 2, 3],
    }]
    _apply_phase_b_merge(theme_map, promoted)
    assert theme_map["x"]["pdfs"] == 5, theme_map["x"]
    assert theme_map["x"]["banks"] == 3, theme_map["x"]
    _ok("pdf count uses max() not sum() — no double counting")


def test_production_code_has_merge_block():
    """Anchor: synthesizer.py contains the merge block (not the old
    behavior of always creating a '(discovered)' suffix)."""
    import inspect
    from report import synthesizer
    src = inspect.getsource(synthesizer)
    assert "reinforced_by_discovery" in src, (
        "production synthesizer must mark merged entries with "
        "reinforced_by_discovery so downstream / QC can see the merge"
    )
    assert "merged_banks = set(existing" in src, (
        "production merge block must compute merged_banks as union of "
        "existing sources + Phase B banks"
    )
    _ok("production synthesizer.py contains the merge block")


if __name__ == "__main__":
    print("=== Phase B merges into Phase A on canonical collision ===")
    test_phase_b_merges_into_phase_a_when_canonical_collides()
    test_phase_b_keeps_discovered_suffix_for_new_canonical()
    test_merge_does_not_double_count_pdfs()
    test_production_code_has_merge_block()
    print("\nALL PHASE B / PHASE A MERGE SMOKE TESTS PASS")
