"""Pilot ledger + card verification.

The ledger feeds the shadow editor, so nondeterminism here reads as
editor variance downstream. The structural rule under test throughout:
GROUPING FILTERS NOTHING — counts inform, they never remove a claim
from the editor's view, because a filter would hide the fragmentation
metric 1 exists to measure.
"""
import json
import sys
import tempfile
from pathlib import Path

from scripts.pilot_ledger import _figure_key, _norm_label, build
from scripts.pilot_verify_cards import verify


def _card(bank, claim, direction="bullish", instruments=("NVDA",),
          tier="top", conviction="medium", anchor="x" * 20):
    return {"bank": bank, "document": "d", "claim": claim,
            "anchor": anchor, "status": "level",
            "instruments": list(instruments), "direction": direction,
            "conviction": conviction, "timeframe": "",
            "_file": f"{bank}.json", "_reader_tier": tier}


# --------------------------------------------------------- hard keys

def test_bank_dedup_within_a_side():
    """Five notes from one bank are ONE voice on a side."""
    cards = [_card("Goldman", f"NVDA rips {i}") for i in range(5)]
    led = build(cards)
    assert len(led["by_instrument"]["NVDA"]["for"]) == 1
    assert led["by_instrument"]["NVDA"]["bank_count"] == 1


def test_opposing_banks_land_on_opposite_sides():
    led = build([_card("Goldman", "NVDA rips", "bullish"),
                 _card("Citi", "NVDA stalls", "bearish")])
    inst = led["by_instrument"]["NVDA"]
    assert [b["bank"] for b in inst["for"]] == ["Goldman"]
    assert [b["bank"] for b in inst["against"]] == ["Citi"]
    assert inst["bank_count"] == 2


def test_figure_key_is_exact_on_digits():
    """$751B and 751 bn share a key; $751B and $757B never do — the
    hard key must not fuzz numbers."""
    assert _figure_key("capex to $751B") == _figure_key("capex 751 bn")
    assert _figure_key("capex $751B") != _figure_key("capex $757B")


def test_macro_cards_get_a_bucket_not_a_drop():
    """A claim with no instrument is macro, not garbage — dropping it
    would be grouping filtering something."""
    led = build([_card("UBS", "core PCE runs hot", instruments=[])])
    assert "_MACRO" in led["by_instrument"]


# ------------------------------------- macro hard key (2026-09-10)

def test_macro_key_is_a_hard_key_across_wording():
    """Spec §4 amendment: a Fed thesis across documents split eight ways
    by topic wording must still meet as ONE macro group, bank-deduped
    per side, so the N-bank check can see it."""
    a = {**_card("GS", "Fed hikes in September", instruments=[]),
         "topic": "fed september hike odds", "macro_key": "FED"}
    b = {**_card("DB", "Fed hiking cycle underpriced", instruments=[]),
         "topic": "fed rate hike pricing", "macro_key": "fed"}
    c = {**_card("DB", "Fed path steeper", instruments=[]),
         "topic": "fed policy path", "macro_key": "FED"}
    d = {**_card("UBS", "gold to 5000", instruments=[]),
         "topic": "gold outlook", "macro_key": "GOLD"}
    led = build([a, b, c, d])
    fed = led["by_instrument"]["_MACRO:FED"]
    assert fed["bank_count"] == 2, fed
    assert len(fed["for"]) == 2, "DB's two cards are one voice"
    assert "_MACRO:GOLD" in led["by_instrument"]
    assert "_MACRO" not in led["by_instrument"], "every keyed macro card left the legacy bucket"
    # the soft labels still fragment visibly: that is metric 1's measurement
    assert len(led["by_topic_label"]) == 4


def test_editor_pack_groups_macro_cards_by_key():
    from scripts.pilot_editor_pack import build_pack
    a = {**_card("GS", "Fed hikes in September", instruments=[]),
         "topic": "fed september hike odds", "macro_key": "FED"}
    b = {**_card("UBS", "legacy macro card", instruments=[])}
    text, meta = build_pack([a, b], {})
    assert "### MACRO · FED — 1 bank(s)" in text
    assert "### MACRO (no instrument) — 1 bank(s)" in text
    assert meta["card_count"] == 2


def test_label_cap_and_macro_key_contract_are_verified():
    from scripts.pilot_config import MACRO_KEYS, MAX_LABELS_PER_DOC
    from scripts.pilot_verify_cards import check_labels, coerce_macro_keys
    cards = [{**_card("GS", f"claim {i}", instruments=[]),
              "topic": f"label {i}", "macro_key": "FED"} for i in range(7)]
    r = check_labels(cards)
    assert r["label_count"] == 7 and r["labels_over_cap"] == 7 - MAX_LABELS_PER_DOC
    assert "at most 5" in r["reask"] and "label 0" in r["reask"]
    # a macro card with no valid key is a contract breach, a tickered card is not
    cards = [{**_card("GS", "no key", instruments=[]), "topic": "t"},
             {**_card("GS", "bad key", instruments=[]), "topic": "t", "macro_key": "FEDERAL"},
             {**_card("GS", "ticker", instruments=["NVDA"]), "topic": "t"}]
    r = check_labels(cards)
    assert r["macro_key_invalid"] == 2 and "macro_key" in r["reask"]
    assert r["labels_over_cap"] == 0
    # the final pass never drops for this: it coerces to OTHER and counts
    assert coerce_macro_keys(cards) == 2
    assert cards[0]["macro_key"] == "OTHER" and cards[1]["macro_key"] == "OTHER"
    assert cards[2]["macro_key"] == ""
    assert "OTHER" in MACRO_KEYS
    # a conforming document produces no re-ask text
    ok = [{**_card("GS", "x", instruments=[]), "topic": "fed", "macro_key": "FED"}]
    assert check_labels(ok)["reask"] == ""


def test_known_labels_lists_the_window_most_used_first():
    from scripts.pilot_known_labels import collect, render
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for day, docs in (("2026-09-10", {"a": ["fed september hike odds"] * 3 + ["gold outlook"]}),
                          ("2026-09-09", {"b": ["fed september hike odds", "yen carry unwind"]}),
                          ("2026-09-01", {"z": ["stale label"]})):
            (root / day).mkdir()
            for name, labels in docs.items():
                (root / day / f"{name}.json").write_text(json.dumps(
                    {"cards": [{"topic": t, "claim": "c"} for t in labels]}), encoding="utf-8")
        rows = collect(str(root), "2026-09-10", days=1)
        assert rows[0] == ("fed september hike odds", 4, 2)
        assert {r[0] for r in rows} == {"fed september hike odds", "gold outlook", "yen carry unwind"}
        assert "stale label" not in render(rows)
        assert "[4 cards, 2 docs]" in render(rows)
        assert "first document" in render(collect(str(root), "2026-08-01", days=0))


# --------------------------------------------------------- soft keys

def test_topic_labels_fold_filler_words():
    assert _norm_label("The AI capex cycle") == _norm_label("AI capex cycle")


def test_reader_topic_label_is_the_soft_key_when_present():
    """Shakedown 2026-09-02: labels derived from claim text fragmented
    48% by construction. Readers now emit `topic`; two banks' different
    claims on the same topic share one label, and a card without the
    field still falls back to its claim."""
    a = {**_card("GS", "AI capex to $751B"), "topic": "AI capex"}
    b = {**_card("MS", "hyperscaler spending accelerates"), "topic": "AI capex"}
    c = _card("UBS", "Legacy card with no topic field")
    led = build([a, b, c])
    labels = led["by_topic_label"]
    assert len(labels) == 2, labels.keys()
    assert any(len(v) == 2 for v in labels.values())


def test_topic_fragmentation_is_visible_not_smoothed():
    """Two labels for the same subject must stay SEPARATE — that
    fragmentation is metric 1's measurement. A ledger that merged them
    would hide the thing the pilot is testing."""
    led = build([_card("GS", "AI capex is accelerating"),
                 _card("MS", "hyperscaler spending is accelerating")])
    assert len(led["by_topic_label"]) == 2


# ------------------------------------------------- filters nothing

def test_every_card_survives_into_the_ledger():
    cards = [_card("GS", "NVDA rips"), _card("GS", "NVDA rips"),
             _card("MS", "AMD lags", "bearish", ["AMD"]),
             _card("UBS", "macro", "neutral", [])]
    led = build(cards)
    assert led["card_count"] == 4, "counts reflect every card"


def test_tier_counts_are_reported():
    """Metrics 2/2a split by reader tier, so the ledger must carry it."""
    led = build([_card("GS", "a", tier="top"),
                 _card("TME", "b", tier="rest")])
    assert led["tier_counts"] == {"rest": 1, "top": 1}


def test_bank_concentration_is_reported():
    led = build([_card("Goldman", "a"), _card("Goldman", "b"),
                 _card("Citi", "c")])
    assert led["bank_concentration"]["Goldman"] > \
        led["bank_concentration"]["Citi"]


# ------------------------------------------------------ verification

_SRC = ("Goldman raised its 2026 hyperscaler capex estimate to $751B "
        "this week, up $80B in two weeks. The 10-year yield broke "
        "4.4% for the first time since May.")


def test_verbatim_anchor_verifies():
    ok, failed, stats = verify(
        [{"anchor": "capex estimate to $751B this week"}], _SRC)
    assert len(ok) == 1 and not failed and stats["matched"] == 1


def test_paraphrased_anchor_fails():
    ok, failed, _ = verify(
        [{"anchor": "Goldman lifted capex to 751 billion dollars"}], _SRC)
    assert not ok and len(failed) == 1
    assert "not found" in failed[0]["_reason"]


def test_short_anchor_fails_here_even_though_the_pulse_only_warns():
    """A card is a CLAIM that must be traceable — '4.4%' traces to
    nothing in particular, so it fails rather than landing in an
    advisory bucket."""
    ok, failed, stats = verify([{"anchor": "4.4%"}], _SRC)
    assert not ok and stats["too_short"] == 1


def test_empty_card_set_is_legitimate():
    ok, failed, stats = verify([], _SRC)
    assert ok == [] and failed == [] and stats["total"] == 0




# ------------------------------------------- reader output salvage

def test_extract_json_handles_the_wrappers_agents_actually_emit():
    """A parse failure costs a document its whole read and shows up in
    metric 5, so salvage belongs in code, not in a prompt plea."""
    from scripts.pilot_finalize_read import extract_json
    want = {"brief": "b", "cards": []}
    assert extract_json('{"brief": "b", "cards": []}') == want
    assert extract_json('```json\n{"brief": "b", "cards": []}\n```') == want
    assert extract_json('Here you go:\n{"brief": "b", "cards": []}\nDone') \
        == want
    assert extract_json("not json at all") is None
    assert extract_json("") is None


def test_finalize_rejects_structural_damage_but_allows_empty_cards():
    """Empty cards is legitimate (an admin note has no checkable
    claims); a missing brief is a failed read. Never invent structure —
    that would put unverified material into the ledger."""
    import subprocess
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        (t / "src.txt").write_text("source", encoding="utf-8")
        prompt = Path("docs/superpowers/routines/pilot/reader.md")

        def run(raw):
            (t / "raw.txt").write_text(raw, encoding="utf-8")
            return subprocess.run(
                [sys.executable, "scripts/pilot_finalize_read.py",
                 "--raw", str(t / "raw.txt"), "--out", str(t / "o.json"),
                 "--source", str(t / "src.txt"), "--tier", "top",
                 "--model", "claude-opus-5", "--prompt", str(prompt)],
                capture_output=True, text=True).returncode

        assert run('{"brief": "a real brief", "cards": []}') == 0
        got = json.loads((t / "o.json").read_text(encoding="utf-8"))
        assert got["provenance"]["model_requested"] == "claude-opus-5"
        assert got["provenance"]["prompt_sha"]
        assert run('{"cards": []}') == 1
        assert run('{"brief": "b", "cards": "oops"}') == 1



# 2026-09-03: every readers run was killed by timeout-minutes: 45 partway
# through the loop, so the end-of-loop ops write never ran and the commit
# step was skipped. 20 runs, 273 verified cards in one of them, all
# discarded. The loop now records and persists after every document, so
# the entry must be upsertable by run id rather than appended.
def test_ops_record_upserts_one_entry_per_run():
    from datetime import datetime, timezone
    from scripts.pilot_ops_record import upsert
    now = datetime(2026, 9, 3, 1, 5, tzinfo=timezone.utc)
    doc = {}
    for n in (1, 2, 3):
        doc = upsert(doc, "run-A", total=n, failed=0, now=now)
    assert len(doc["runs"]) == 1, doc["runs"]
    assert doc["runs"][0]["total"] == 3 and doc["runs"][0]["run_id"] == "run-A"
    doc = upsert(doc, "run-B", total=2, failed=1, now=now)
    assert len(doc["runs"]) == 2
    # the day's rate spans every run
    assert doc["reader_failure_rate"] == round(1 / 5, 3)


def test_ops_record_flags_a_read_inside_the_pulse_window():
    from datetime import datetime, timezone
    from scripts.pilot_ops_record import upsert
    inside = datetime(2026, 9, 3, 14, 0, tzinfo=timezone.utc)
    outside = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    d = upsert({}, "r", total=3, failed=0, now=inside)
    assert d["runs"][0]["in_pulse_window"] and d["collided_with_pulse_window"]
    d2 = upsert({}, "r", total=3, failed=0, now=outside)
    assert not d2["runs"][0]["in_pulse_window"] and not d2["collided_with_pulse_window"]
    # a QUIET run inside the window is not a collision: it read nothing
    d3 = upsert({}, "r", total=0, failed=0, now=inside, quiet=True)
    assert d3["runs"][0]["quiet"] is True and not d3["collided_with_pulse_window"]
    assert d3["reader_failure_rate"] is None


def test_ops_record_survives_a_corrupt_or_partial_file():
    from datetime import datetime, timezone
    from scripts.pilot_ops_record import upsert
    now = datetime(2026, 9, 3, 1, 5, tzinfo=timezone.utc)
    for junk in ({}, {"runs": None}, {"runs": ["not-a-dict", {"run_id": "x", "total": 1, "failed": 0}]}):
        d = upsert(dict(junk), "run-A", total=4, failed=0, now=now)
        assert any(r["run_id"] == "run-A" for r in d["runs"]), d


# 2026-09-04: two reader runs failed every document with "not parseable
# JSON" and nothing else. The CLI reports usage limits, auth and
# max-turns on STDOUT; the workflow tailed stderr, so the cause was
# unrecoverable. finalize_read now shows what actually came back.
def test_finalize_read_failure_shows_the_head_of_what_the_reader_said():
    import os
    import subprocess
    import sys as _sys
    import tempfile
    d = tempfile.mkdtemp()
    raw = os.path.join(d, "raw.txt")
    with open(raw, "w", encoding="utf-8") as fh:
        fh.write("Claude usage limit reached. Your limit will reset at 7pm (UTC).\n")
    src = os.path.join(d, "src.txt"); open(src, "w").close()
    r = subprocess.run(
        [_sys.executable, "scripts/pilot_finalize_read.py", "--raw", raw,
         "--out", os.path.join(d, "cards.json"), "--source", src, "--tier", "top",
         "--model", "m", "--prompt", "docs/superpowers/routines/pilot/reader.md"],
        capture_output=True, text=True)
    assert r.returncode == 1
    assert "not parseable JSON" in r.stdout
    assert "usage limit reached" in r.stdout, r.stdout
    assert not os.path.exists(os.path.join(d, "cards.json"))

if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")
