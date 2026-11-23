"""Sentences the citation check fails are dropped before the Omnipulse
body is published (owner, 2026-11-23).

Until then a failed sentence was recorded in the meta and published. The
sentences below are the real ones from the replay of the published days."""
import json
import os
import subprocess
import sys

from scripts import omnipulse_body as O
from scripts.pilot_verify_citations import drop_failed, strip_markers, verify

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _pack(*cards, docs=None):
    return {"cards": {f"c{i}": {"claim": c, "anchor": c, "bank": b} for i, (c, b) in enumerate(cards, 1)},
            "docs": docs or {}, "card_count": len(cards)}


BTC = _pack(("Bitcoin has lost roughly a quarter of its value since October 21.", "The Market Ear"),
            ("Bitcoin's selling accelerated after BTC broke below its 200-day moving average around $73,000.",
             "The Market Ear"),
            ("Spot bitcoin ETFs have seen seven straight days of selling.", "The Market Ear"))
BTC_MD = ("# Headline\n\n## 2. THE MAIN EVENT\n\n### Bitcoin\n\n"
          "$BTC has lost roughly a quarter of its value since October 21 [c1]. "
          "The weekend flush triggered nearly $2 billion of forced closures after the price broke below "
          "its 200-day average around $73,000 [c2]. "
          "Fund flows have been one-way, with seven straight days of selling [c3].\n\n"
          "## 3. BRIEFS\n\n### B\n\nText.\n\n### C\n\nText.\n")


def test_a_sentence_with_a_figure_no_card_carries_is_removed_whole():
    md, dropped, left = drop_failed(BTC_MD, BTC)
    assert left == 0 and len(dropped) == 1
    assert dropped[0]["sentence"].startswith("The weekend flush triggered nearly $2 billion")
    assert dropped[0]["reason"] == "figures not in cited card(s): ['2000000000']"
    assert "$2 billion" not in md and "$73,000" not in md
    # its neighbours are untouched and joined by one space
    assert ("since October 21 [c1]. Fund flows have been one-way, with seven straight days "
            "of selling [c3].") in md
    assert verify(md, BTC)["failures"] == []
    assert O.problems(strip_markers(md), {}) == []


def test_a_clean_edition_is_returned_unchanged():
    md = BTC_MD.replace("The weekend flush triggered nearly $2 billion of forced closures after the price "
                        "broke below its 200-day average around $73,000 [c2]. ", "")
    assert drop_failed(md, BTC) == (md, [], 0)


def test_a_paragraph_and_a_theme_left_empty_go_with_their_heading():
    pack = _pack(("Goldman sees the level holding.", "Goldman Sachs"),
                 ("UBS expects a cut in March.", "UBS"))
    md = ("# H\n\n## 2. THE MAIN EVENT\n\n### Main\n\nGoldman sees the level holding [c1].\n\n"
          "## 3. BRIEFS\n\n### One\n\nUBS expects a cut in March [c2].\n\n"
          "### Two\n\nVolumes rose 41% on the week.\n\n### Three\n\nUBS expects a cut in March [c2].\n")
    out, dropped, left = drop_failed(md, pack)
    assert left == 0 and [d["sentence"] for d in dropped] == ["Volumes rose 41% on the week."]
    assert "### Two" not in out and "### One" in out and "### Three" in out
    assert "\n\n\n" not in out


def test_dropping_a_banks_only_citation_drops_the_sentences_that_leaned_on_it():
    """Second round: the uncited opener passes only while its theme cites
    the bank."""
    pack = _pack(("Goldman's desk bought semiconductors on the dip.", "Goldman Sachs"),
                 ("UBS says the rebound lacks breadth.", "UBS"))
    md = ("# H\n\n## 2. THE MAIN EVENT\n\n### Main\n\nGoldman is buying the rebound. "
          "Its desk bought $4 billion of semiconductors on the dip [c1]. UBS will not follow. "
          "It says the rebound lacks breadth [c2].\n\n## 3. BRIEFS\n\n### B\n\nText.\n\n### C\n\nText.\n")
    out, dropped, left = drop_failed(md, pack)
    assert left == 0
    assert [d["sentence"] for d in dropped] == [
        "Its desk bought $4 billion of semiconductors on the dip [c1].", "Goldman is buying the rebound."]
    assert "Goldman" not in out and "UBS will not follow. It says the rebound lacks breadth [c2]." in out


def test_a_name_with_initials_does_not_cut_a_sentence_in_two():
    """2026-09-03: "...the level J.P. Morgan's desk flags as support" was
    read as two sentences. Dropping one would publish half a sentence."""
    from scripts.pilot_verify_citations import split_sentences
    s = "It sits at $4,309 [c1], with $4,300 the level J.P. Morgan's desk flags as support [c2]. Gold fell."
    assert split_sentences(s) == [
        "It sits at $4,309 [c1], with $4,300 the level J.P. Morgan's desk flags as support [c2].", "Gold fell."]
    assert split_sentences("J.P. Morgan expects one hike a quarter [c1]. UBS agrees [c2].") == [
        "J.P. Morgan expects one hike a quarter [c1].", "UBS agrees [c2]."]
    pack = _pack(("Gold sits at $4,309.", "JPMorgan"), ("JPMorgan's desk flags support.", "JPMorgan"))
    md = "# H\n\n## 2. THE MAIN EVENT\n\n### Gold\n\nGold is lower. " + s + "\n"
    out, dropped, left = drop_failed(md, pack)
    assert left == 0 and [d["sentence"] for d in dropped] == [s[:-len(" Gold fell.")]]
    assert out.endswith("### Gold\n\nGold is lower. Gold fell.\n")


def test_several_failures_in_one_sentence_are_one_drop_with_every_reason():
    pack = _pack(("Goldman sees the level holding.", "Goldman Sachs"))
    md = ("# H\n\n## 2. THE MAIN EVENT\n\n### Main\n\nGoldman sees the level holding [c1]. "
          "Morgan Stanley puts fair value at 5,400 [c1] [c9].\n")
    _out, dropped, left = drop_failed(md, pack)
    assert left == 0 and len(dropped) == 1
    for part in ("card does not exist", "bank named", "figures not in cited card(s): ['5400']"):
        assert part in dropped[0]["reason"]


def test_the_leans_block_and_headings_are_never_touched():
    md = BTC_MD + "\n## _LEANS\n\n- BTC: down 25% [c99]\n"
    out, dropped, _left = drop_failed(md, BTC)
    assert len(dropped) == 1 and out.endswith("## _LEANS\n\n- BTC: down 25% [c99]\n")
    assert out.startswith("# Headline\n\n## 2. THE MAIN EVENT\n\n### Bitcoin\n")


# ------------------------------------------------- what production accepts

def _meta(**cit):
    return {"citations": cit}


def test_production_refuses_a_body_whose_failures_were_not_dropped():
    clean = strip_markers(BTC_MD)
    fail = [{"sentence": "x", "cite": "c2", "reason": "figures not in cited card(s): ['2000000000']"}]
    assert O.problems(clean, _meta(failures=[])) == []
    assert O.problems(clean, _meta(failures=fail)) == [
        "1 sentence(s) failed the citation check and were not dropped"]
    assert O.problems(clean, _meta(failures=fail, dropped=[{"sentence": "x", "reason": "y"}],
                                   dropped_share=0.012, remaining_failures=0)) == []
    assert O.problems(clean, _meta(failures=fail, dropped=[], dropped_share=0.0, remaining_failures=2)) == [
        "2 sentence(s) still fail the citation check after the drop"]


def test_production_refuses_a_body_that_lost_too_much():
    clean = strip_markers(BTC_MD)
    fail = [{"sentence": "x", "cite": "", "reason": "r"}]
    at_limit = _meta(failures=fail, dropped=fail, dropped_share=O.MAX_DROP_SHARE, remaining_failures=0)
    over = _meta(failures=fail, dropped=fail, dropped_share=0.22, remaining_failures=0)
    assert O.problems(clean, at_limit) == []
    assert O.problems(clean, over) == ["the citation check dropped 22% of the body"]


# ------------------------------------------------------------ the command

def _run(tmp_path, *flags):
    (tmp_path / "s.md").write_text(BTC_MD, encoding="utf-8")
    (tmp_path / "p.json").write_text(json.dumps(BTC), encoding="utf-8")
    r = subprocess.run(
        [sys.executable, os.path.join(REPO, "scripts", "pilot_verify_citations.py"),
         str(tmp_path / "s.md"), str(tmp_path / "p.json"), "--meta-out", str(tmp_path / "m.json"),
         "--stripped-out", str(tmp_path / "c.md"), "--final", *flags],
        capture_output=True, text=True, cwd=REPO)
    assert r.returncode == 0, r.stderr
    return ((tmp_path / "c.md").read_text(encoding="utf-8"),
            json.loads((tmp_path / "m.json").read_text(encoding="utf-8"))["citations"])


def test_the_final_pass_writes_the_clean_file_without_the_failed_sentence(tmp_path):
    clean, cit = _run(tmp_path, "--drop-failed")
    assert "forced closures" not in clean and "[c" not in clean
    assert "a quarter of its value since October 21. Fund flows have been one-way" in clean
    # the record keeps what the editor got wrong and says what was removed
    assert len(cit["failures"]) == 1 and len(cit["dropped"]) == 1
    assert cit["remaining_failures"] == 0 and 0 < cit["dropped_share"] < 0.5
    assert O.problems(clean, {"citations": dict(cit, dropped_share=0.02)}) == []


def test_without_the_flag_nothing_is_dropped_and_production_refuses_the_body(tmp_path):
    clean, cit = _run(tmp_path)
    assert "forced closures" in clean and "dropped" not in cit
    assert any("were not dropped" in p for p in O.problems(clean, {"citations": cit}))


def test_the_editor_workflow_drops_on_its_final_pass():
    wf = open(os.path.join(REPO, ".github", "workflows", "pilot-editor.yml"), encoding="utf-8").read()
    final = [l for l in wf.splitlines() if "--final" in l and "--stripped-out" in l]
    assert len(final) == 1 and "--drop-failed" in final[0]
    # the re-ask pass still reports failures to the editor and drops nothing
    reask = [l for l in wf.splitlines() if "--reask-out /tmp/ed/reask.json" in l]
    assert len(reask) == 1 and "--drop-failed" not in reask[0]
