"""The published pulse is built by code after the gates pass, and the
bridge checks that the file it received is that text (2026-11-23).

Found by replaying the stored gate trails: the routine's model types out
every file it pushes, the trail included (ten of 51 said so: "abridged by
the routine for transport"), so a trail on the branch is the model's own
account. The seal rides in the pulse file instead."""
import asyncio
import json
from pathlib import Path

import pytest

from github_bridge import jobs as J
from github_bridge import pulse_gate
from scripts import pulse_driver as PD
from scripts import pulse_seal as S

REPO = Path(__file__).resolve().parent.parent
BODY = "# Headline\n\n## 1. RECAP\n\n" + ("Stocks rose on the open. " * 120) + "\n\n## _LEANS\n\n- SPX: up\n"
GATES = ["holiday", "volume", "omnipulse", "draft_validate", "lint", "final_validate", "strip", "adversarial"]


def _fire(tmp_path, mode="classic", **files):
    (tmp_path / "final.md").write_text(BODY, encoding="utf-8")
    (tmp_path / "draft.md").write_text(BODY, encoding="utf-8")
    (tmp_path / "ctx.json").write_text(json.dumps({"pdf_count": 212, "dumped_at_utc": "2026-11-24T13:40:00Z"}),
                                       encoding="utf-8")
    (tmp_path / "pulse_mode.txt").write_text(mode, encoding="utf-8")
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    d = PD.Driver(tmp_path)
    for g in GATES:
        d._decide(g, "CONTINUE")
    return d


# ------------------------------------------------------------------ the seal

def test_a_stamped_file_passes_and_a_changed_word_fails():
    text = S.stamp("---\npdf_count: 3\nbody_source: classic\n---\n\n# H\n\nGoldman sees 5,400.\n")
    assert S.read_seal(text) and S.problem(text) is None
    assert "differs from the text the checks passed" in S.problem(text.replace("5,400", "5,600"))
    # the frontmatter is covered too: a test fire relabelled as production
    assert S.problem(text.replace("body_source: classic", "body_source: omnipulse"))


def test_a_file_with_no_seal_fails():
    assert "no gate seal" in S.problem("---\npdf_count: 3\n---\n\n# H\n")
    assert "no gate seal" in S.problem("# H\n\nno frontmatter at all\n")
    # a seal line in the body is prose, not a seal
    assert "no gate seal" in S.problem("---\npdf_count: 3\n---\n\n# H\n\ngate_seal: abc\n")


def test_what_a_faithful_copy_changes_does_not_break_the_seal():
    text = S.stamp("---\npdf_count: 3\n---\n\n# H\n\nLine one.\n\nLine two.\n")
    for copy in (text.replace("\n", "\r\n"), text.rstrip("\n"), text + "\n\n",
                 text.replace("Line one.\n", "Line one.   \n"), text.replace("\n\nLine two", "\n\n\nLine two")):
        assert S.problem(copy) is None, repr(copy[-30:])


def test_stamping_twice_keeps_one_seal():
    once = S.stamp("---\na: 1\n---\n\nbody\n")
    assert S.stamp(once) == once and once.count(S.SEAL_KEY + ":") == 1


def test_the_archive_carries_the_verdict_not_the_seal():
    text = S.stamp("---\na: 1\n---\n\nbody\n")
    assert "gate_check: pass\n---" in S.mark_checked(text, True) and S.SEAL_KEY not in S.mark_checked(text, True)
    assert "a: 1\ngate_check: failed\n---" in S.mark_checked("---\na: 1\n---\n\nbody\n", False)


# ------------------------------------------------------ compose, in the driver

def test_compose_builds_the_file_only_after_a_preflight_pass(tmp_path):
    d = _fire(tmp_path)
    assert d.compose() == "BLOCK" and not (tmp_path / "final_with_frontmatter.md").exists()
    assert d.preflight() == "PASS"
    assert d.compose() == "PASS"
    text = (tmp_path / "final_with_frontmatter.md").read_text(encoding="utf-8")
    assert S.problem(text) is None
    head, body = text.split("\n---\n", 1)
    assert "pdf_count: 212" in head and "body_source: classic" in head
    assert "dumped_at_utc: 2026-11-24T13:40:00Z" in head and "target_channels" not in head
    assert body.strip() == BODY.strip()


def test_compose_refuses_a_final_changed_after_the_pass(tmp_path):
    """The edit after the last check: preflight passed one text, another
    was about to be published."""
    d = _fire(tmp_path)
    assert d.preflight() == "PASS"
    (tmp_path / "final.md").write_text(BODY.replace("rose", "fell"), encoding="utf-8")
    assert d.compose() == "BLOCK"
    assert "changed after the preflight PASS" in d.state["gates"]["compose"]["detail"]
    assert not (tmp_path / "final_with_frontmatter.md").exists()
    # run the gates again, pass again, and it composes
    assert d.preflight() == "PASS" and d.compose() == "PASS"


def test_a_block_after_a_pass_withdraws_the_pass(tmp_path):
    d = _fire(tmp_path)
    assert d.preflight() == "PASS"
    (tmp_path / "final.md").write_text("# tiny\n", encoding="utf-8")
    assert d.preflight() == "BLOCK" and d.compose() == "BLOCK"


def test_compose_stamps_the_light_reason_the_residual_count_and_the_test_channel(tmp_path, monkeypatch):
    monkeypatch.delenv("TARGET_CHANNELS", raising=False)
    monkeypatch.setattr(PD.Driver, "_body_markers", lambda self: ["# Headline"])
    d = _fire(tmp_path, mode="light", **{"light_reason.txt": "not published\nafter 600s",
                                         "adversarial_residuals.json": "[1, 2]",
                                         "target_channels.txt": "test-channel\n"})
    assert d.preflight() == "PASS" and d.compose() == "PASS"
    head = (tmp_path / "final_with_frontmatter.md").read_text(encoding="utf-8").split("\n---\n", 1)[0]
    assert "body_source: light" in head and "light_reason: not published after 600s" in head
    assert "adversarial_residuals: 2" in head and "target_channels: test-channel" in head
    # the reason is stamped on a light day only
    d2 = _fire(tmp_path, mode="classic", **{"light_reason.txt": "stale"})
    assert d2.preflight() == "PASS" and d2.compose() == "PASS"
    assert "light_reason" not in (tmp_path / "final_with_frontmatter.md").read_text(encoding="utf-8")


def test_the_final_gate_uses_the_validators_own_hard_list():
    """The driver kept a second list: it named a kind no validator emits
    and left out the released-print check."""
    from scripts.pulse_draft_validate import HARD_VIOLATION_KINDS
    kinds = PD.final_gate_hard_kinds()
    assert kinds == set(HARD_VIOLATION_KINDS)
    assert "released-actual-missing" in kinds and "released-figure-mismatch" not in kinds
    assert not hasattr(PD, "FINAL_GATE_HARD_KINDS")


def test_a_print_dropped_after_the_draft_dispatches_the_fixup(tmp_path, monkeypatch):
    d = _fire(tmp_path)
    (tmp_path / "draft_validation.json").write_text(json.dumps({"violations": []}), encoding="utf-8")

    def fake_run(self, args):
        (tmp_path / "final_validation.json").write_text(json.dumps({"violations": [
            {"kind": "released-actual-missing", "severity": "hard", "detail": "CPI printed, no actual"}]}),
            encoding="utf-8")
        return 3, ""
    monkeypatch.setattr(PD.Driver, "_run", fake_run)
    assert d.gate_final_validate() == "DISPATCH_FIXUP"


# ------------------------------------------------------------- the bridge

def _sealed():
    return S.stamp("---\npdf_count: 3\nbody_source: classic\n---\n\n" + BODY)


def test_the_verdict_by_mode(monkeypatch):
    good, bad = _sealed(), _sealed().replace("rose", "fell", 1)
    for m, want_bad in (("record", "post"), ("enforce", "refuse"), ("off", "post")):
        monkeypatch.setattr(pulse_gate, "mode", lambda m=m: m)
        assert pulse_gate.verdict(good) == ("post", [])
        action, problems = pulse_gate.verdict(bad)
        assert action == want_bad and bool(problems) == (m != "off")


def test_a_fault_in_the_check_posts_and_reports(monkeypatch):
    monkeypatch.setattr(pulse_gate, "mode", lambda: "enforce")
    monkeypatch.setattr(pulse_gate, "check", lambda raw: 1 / 0)
    action, problems = pulse_gate.verdict(_sealed())
    assert action == "post" and "the gate check itself failed" in problems[0]


def test_the_default_mode_is_record_and_an_unknown_value_is_record(monkeypatch):
    from config import settings
    assert pulse_gate.mode() == "record"
    monkeypatch.setattr(settings, "pulse_gate_mode", "Enforce ")
    assert pulse_gate.mode() == "enforce"
    monkeypatch.setattr(settings, "pulse_gate_mode", "strict")
    assert pulse_gate.mode() == "record"


class _Stop(Exception):
    pass


def _process(monkeypatch, raw, gate_mode):
    """Run the bridge on one pending file up to the point it would format
    and post it. Returns (wrote, deleted, pages, reached_posting)."""
    wrote, deleted, pages = {}, [], []
    J._GATE_REPORTED.clear()
    monkeypatch.setattr(pulse_gate, "mode", lambda: gate_mode)
    monkeypatch.setattr(J.gh, "get_file_text", lambda path: raw if path.endswith("x.md") else "")
    monkeypatch.setattr(J.gh, "put_file", lambda path, body, msg: wrote.__setitem__(path, body))
    monkeypatch.setattr(J.gh, "delete_file", lambda path, msg: deleted.append(path))

    async def fake_alert(text, dedupe_key=None):
        pages.append(text)
    monkeypatch.setattr("discord_bot.ops_alert.ops_alert", fake_alert)

    seen = {}

    def stop(markdown):
        seen["raw"] = markdown
        raise _Stop()
    monkeypatch.setattr(J, "_parse_frontmatter", stop)
    asyncio.run(J._process_one_pulse(object(), {"name": "x.md"}))
    return wrote, deleted, pages, seen.get("raw")


def test_record_mode_pages_writes_a_marker_and_still_posts(monkeypatch):
    wrote, deleted, pages, raw = _process(monkeypatch, _sealed().replace("rose", "fell", 1), "record")
    assert raw is not None and "gate_check: failed" in raw and not deleted
    assert len(pages) == 1 and "posted anyway (record mode)" in pages[0]
    marker = wrote[f"{J.QC_REVIEWS_DIR}/x.gate.md"]
    assert "POSTED WITH A FAILED GATE CHECK" in marker and "differs from the text the checks passed" in marker
    assert J.DELIVERY_FAILED_DIR + "/x.md" not in wrote


def test_enforce_mode_moves_the_pulse_aside_and_does_not_post(monkeypatch):
    raw_in = "---\npdf_count: 3\n---\n\n" + BODY          # the model wrote the file itself: no seal
    wrote, deleted, pages, raw = _process(monkeypatch, raw_in, "enforce")
    assert raw is None, "nothing past the gate ran"
    assert wrote[J.DELIVERY_FAILED_DIR + "/x.md"] == raw_in and deleted == [J.PENDING_DIR + "/x.md"]
    assert "REFUSED, not posted" in pages[0] and "no gate seal" in pages[0]
    assert "REFUSED" in wrote[f"{J.QC_REVIEWS_DIR}/x.gate.md"]


def test_a_sealed_pulse_goes_through_marked_pass_with_no_page(monkeypatch):
    for m in ("record", "enforce"):
        wrote, deleted, pages, raw = _process(monkeypatch, _sealed(), m)
        assert raw is not None and "gate_check: pass" in raw and S.SEAL_KEY not in raw
        assert not wrote and not deleted and not pages


def test_a_pulse_that_stays_pending_is_reported_once(monkeypatch):
    bad = _sealed().replace("rose", "fell", 1)
    wrote, _d, pages, _r = _process(monkeypatch, bad, "record")
    assert len(pages) == 1
    monkeypatch.setattr(J, "_parse_frontmatter", lambda md: (_ for _ in ()).throw(_Stop()))
    asyncio.run(J._process_one_pulse(object(), {"name": "x.md"}))
    assert len(pages) == 1 and len(wrote) == 1


# ------------------------------------------------------------ the routine

def test_the_routine_composes_with_the_driver_and_the_dead_script_is_gone():
    md = (REPO / "docs/superpowers/routines/synthesis-routine.md").read_text(encoding="utf-8")
    step6 = md[md.index("## STEP 6"):md.index("## STEP 7")]
    order = [step6.index(f"pulse_driver.py {c}") for c in ("preflight", "compose", "push_list")]
    assert order == sorted(order)
    assert "exactly as it is in `/tmp/push_files.json`" in step6
    # the 340-line script the model was told to read and not run
    assert "PYEOF" not in step6 and "frontmatter_lines" not in md
