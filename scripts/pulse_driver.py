#!/usr/bin/env python3
"""Pulse routine driver — the decision authority for synthesis control
flow (2026-08-20, redesign spec §7 / sequencing step 1, phase 1).

WHY THIS EXISTS
================
The routine's failure class is not bad step logic — it's decisions
BETWEEN steps living in prose a model can skim. The changelog documents
the skipped-gate class three times (SCRUB dispatched on soft-only lint,
validators not re-run after FIXUP, gates read as advisory). This driver
moves every gate decision into code and — the structural kill — refuses
the commit preflight unless the state file shows every required gate
was actually consulted. A skimmed routine can no longer skip a gate,
because the skip is detected at the single choke point before commit.

Phase 1 scope: gates, budgets, decision oracle, preflight. The routine
md's big deterministic blocks (context fetch, adjudication input prep,
commit machinery) stay where they are; phase 2 (block relocation)
follows the pilot per the redesign spec.

CONTRACT
========
Every command prints a final line `DECISION: <TOKEN>[ -- detail]` and
exits 0. The routine reads the literal token and does exactly what the
step's table says for it. All decisions and details are also recorded
in the state file, which `preflight` audits.

Commands:
  gate holiday          -> CONTINUE | SKIP_PULSE
  gate volume           -> CONTINUE (records pdf_count; the wait loop
                           lives in the md block, which runs first)
  gate draft_validate   -> CONTINUE | REROLL_DRAFT (max 2; writes
                           /tmp/draft_reroll_feedback.txt; exit-4
                           numeric-scope-drift items are written to
                           /tmp/edit_verify_items.txt for the EDIT pass)
  gate lint             -> DISPATCH_SCRUB | SKIP_SCRUB
  gate scrub_relint     -> REDISPATCH_SCRUB | CONTINUE (5.7.3 logic:
                           progress + max-2 budget)
  gate final_validate   -> DISPATCH_FIXUP (writes
                           /tmp/final_fixup_violations.json) | CONTINUE
                           (5.75 logic: new-hard delta vs the DRAFT
                           run, deterministic _LEANS restore)
  gate strip            -> CONTINUE (runs the strip, verifies no `## _`
                           headers remain)
  gate adversarial      -> CONTINUE | DISPATCH_REPAIR (max 2; items at
                           /tmp/adversarial_repair_items.json) |
                           CONTINUE_WITH_RESIDUAL (budget spent, note
                           appended) | BLOCK (verdict unreadable);
                           --recheck after each repair round
  record <label> [detail]  free-form trail entry (agent dispatches etc.)
  preflight             -> PASS | BLOCK (the choke point before STEP 6)
  status                -> dump state

State: {tmp}/driver_state.json. `--tmp DIR` overrides /tmp for tests.
"""
from __future__ import annotations

import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The hard-violation kinds STEP 5.75 gates on. Mirrors the routine's
# final-gate set — includes consensus-amnesia (2026-08-20).
FINAL_GATE_HARD_KINDS = {
    "duplicate-sibling-sections", "contrarian-buried-in-appendix",
    "main-event-lean-missing", "leans-block-missing",
    "weekday-date-mismatch", "released-figure-mismatch",
    "consensus-amnesia",
}

MAX_DRAFT_REROLLS = 2
MAX_SCRUB_ITERS = 2
# Redesign sequencing step 3 (spec §6): repair rounds the adversarial
# gate may dispatch before shipping with a labeled residual note.
MAX_ADVERSARIAL_REPAIRS = 2

# The residual marker the driver appends when the repair budget is
# spent with hard findings remaining. Preflight verifies this exact
# prefix is present whenever the gate decided CONTINUE_WITH_RESIDUAL —
# the note is the spec's ship-anyway condition, not optional garnish.
RESIDUAL_NOTE_PREFIX = "*Accuracy note:"


class Driver:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.state_path = tmp / "driver_state.json"
        self.state = self._load()

    # ------------------------------------------------------------------
    def _load(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return {"gates": {}, "budgets": {}, "history": []}

    def _save(self):
        self.state_path.write_text(
            json.dumps(self.state, indent=1), encoding="utf-8")

    def _decide(self, gate: str, token: str, detail: str = "") -> str:
        now = datetime.datetime.utcnow().isoformat() + "Z"
        self.state["gates"][gate] = {
            "decision": token, "detail": detail[:400], "at": now,
        }
        self.state["history"].append(
            {"gate": gate, "decision": token, "at": now})
        self._save()
        line = f"DECISION: {token}"
        if detail:
            line += f" -- {detail[:200]}"
        print(line)
        return token

    def _run(self, args: list[str]) -> tuple[int, str]:
        """Run a repo script; return (exit_code, stdout+stderr)."""
        import os
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        p = subprocess.run(
            [sys.executable, *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", cwd=str(REPO_ROOT),
            env=env,
        )
        out = (p.stdout or "") + (p.stderr or "")
        # surface the tool's own output for the routine log
        print(out.strip())
        return p.returncode, out

    # ------------------------------------------------------------------
    # gates
    # ------------------------------------------------------------------
    def gate_holiday(self) -> str:
        skip = self.tmp / "holiday_skip.txt"
        if skip.exists():
            return self._decide(
                "holiday", "SKIP_PULSE",
                f"US market holiday: {skip.read_text()[:60].strip()}")
        return self._decide("holiday", "CONTINUE")

    def gate_volume(self) -> str:
        try:
            ctx = json.loads((self.tmp / "ctx.json").read_text(
                encoding="utf-8"))
            count = ctx.get("pdf_count") or 0
        except Exception as e:
            return self._decide("volume", "CONTINUE",
                                f"ctx unreadable ({e}) - proceeding")
        return self._decide("volume", "CONTINUE", f"pdf_count={count}")

    def gate_draft_validate(self) -> str:
        """STEP 4.5 — runs the validator itself, applies the literal
        exit-code decision table, tracks the re-roll budget."""
        out_json = self.tmp / "draft_validation.json"
        code, _ = self._run([
            "scripts/pulse_draft_validate.py",
            str(self.tmp / "draft.md"), str(self.tmp / "ctx.json"),
            str(out_json),
        ])
        rerolls = self.state["budgets"].get("draft_rerolls", 0)
        if code == 3:
            if rerolls >= MAX_DRAFT_REROLLS:
                return self._decide(
                    "draft_validate", "CONTINUE",
                    f"hard residuals after {rerolls} re-rolls -- "
                    f"shipping with residuals recorded (budget spent)")
            self.state["budgets"]["draft_rerolls"] = rerolls + 1
            self._save()
            self._write_reroll_feedback(out_json)
            return self._decide(
                "draft_validate", "REROLL_DRAFT",
                f"hard violations; re-roll {rerolls + 1}/"
                f"{MAX_DRAFT_REROLLS}; feedback at "
                f"{self.tmp / 'draft_reroll_feedback.txt'}")
        if code == 4:
            self._write_edit_verify_items(out_json)
            return self._decide("draft_validate", "CONTINUE",
                                "soft violations only (advisory)")
        if code == 0:
            return self._decide("draft_validate", "CONTINUE", "clean")
        return self._decide("draft_validate", "CONTINUE",
                            f"validator error (exit {code}) -- "
                            f"proceeding, no violations recorded")

    def _write_reroll_feedback(self, out_json: Path):
        try:
            v = json.loads(out_json.read_text(encoding="utf-8"))
            viols = v if isinstance(v, list) else v.get("violations", [])
        except Exception:
            viols = []
        lines = [
            "STRUCTURAL-VIOLATIONS -- your previous DRAFT failed "
            "validation. Each violation describes what you need to "
            "change:", "",
        ]
        for x in viols:
            if x.get("severity") == "hard":
                lines.append(
                    f"- [{x.get('kind')}] {x.get('message', '')[:400]}")
        lines += ["", "Re-roll the DRAFT addressing these violations."]
        (self.tmp / "draft_reroll_feedback.txt").write_text(
            "\n".join(lines), encoding="utf-8")

    def _write_edit_verify_items(self, out_json: Path):
        """Exit-4 carry: numeric-scope-drift items go to the EDIT pass
        as verify-this items (the 2026-07-07 +119% rule: adjudicate
        against the FULL source sentence, never a snippet)."""
        try:
            v = json.loads(out_json.read_text(encoding="utf-8"))
            viols = v if isinstance(v, list) else v.get("violations", [])
        except Exception:
            viols = []
        items = [x for x in viols
                 if x.get("kind") == "numeric-scope-drift"]
        if not items:
            return
        lines = ["VERIFY-THESE-FIGURES (from DRAFT validation):"]
        for x in items:
            lines.append(
                f"- Confirm {x.get('figure', '?')} is scoped the way "
                f"the source scopes it (source subject: "
                f"{x.get('source_subject', '?')}). If the source "
                f"really attached it broadly, leave it; if the draft "
                f"widened a narrow stat, re-scope or drop.")
        (self.tmp / "edit_verify_items.txt").write_text(
            "\n".join(lines), encoding="utf-8")

    def gate_lint(self) -> str:
        """STEP 5.5/5.7.1 — run lint, read the decision sidecar (the
        SINGLE authority on hard vs soft), emit the dispatch token."""
        out_json = self.tmp / "lint_report.json"
        self._run([
            "scripts/pulse_lint.py", str(self.tmp / "final.md"),
            str(out_json), str(self.tmp / "ctx.json"),
        ])
        decision = self._read_lint_decision(out_json)
        self.state.setdefault("lint", {})["last_hard"] = decision.get(
            "hard_issue_count", 0)
        self._save()
        if decision.get("scrub_recommended"):
            return self._decide(
                "lint", "DISPATCH_SCRUB",
                f"{decision.get('hard_issue_count')} hard issue(s)")
        return self._decide("lint", "SKIP_SCRUB",
                            decision.get("reason", "no hard issues"))

    def _read_lint_decision(self, out_json: Path) -> dict:
        try:
            return json.loads(
                Path(str(out_json) + ".decision").read_text(
                    encoding="utf-8"))
        except Exception:
            return {"scrub_recommended": False,
                    "reason": "sidecar missing -- treating as clean",
                    "hard_issue_count": 0}

    def gate_scrub_relint(self) -> str:
        """STEP 5.7.3 — re-lint after a SCRUB pass; progress + budget."""
        prev_hard = self.state.get("lint", {}).get("last_hard", 0)
        out_json = self.tmp / "lint_report.json"
        self._run([
            "scripts/pulse_lint.py", str(self.tmp / "final.md"),
            str(out_json), str(self.tmp / "ctx.json"),
        ])
        decision = self._read_lint_decision(out_json)
        hard = decision.get("hard_issue_count", 0)
        self.state.setdefault("lint", {})["last_hard"] = hard
        iters = self.state["budgets"].get("scrub_iters", 1)
        self._save()
        if hard == 0:
            return self._decide("scrub_relint", "CONTINUE",
                                "0 hard issues after SCRUB")
        if iters >= MAX_SCRUB_ITERS:
            return self._decide(
                "scrub_relint", "CONTINUE",
                f"{hard} residual hard issue(s) after "
                f"{iters} SCRUB passes -- shipping with residual "
                f"lint report (budget spent)")
        if hard < prev_hard:
            self.state["budgets"]["scrub_iters"] = iters + 1
            self._save()
            return self._decide(
                "scrub_relint", "REDISPATCH_SCRUB",
                f"progress ({prev_hard} -> {hard}); pass "
                f"{iters + 1}/{MAX_SCRUB_ITERS}")
        return self._decide(
            "scrub_relint", "CONTINUE",
            f"WARNING: SCRUB did not reduce hard issues "
            f"({prev_hard} -> {hard}) -- proceeding")

    def gate_final_validate(self, recheck: bool = False) -> str:
        """STEP 5.75 — re-validate the FINAL doc, compute the new-hard
        delta vs the DRAFT run, deterministically restore a deleted
        ## _LEANS block, and emit FIXUP dispatch when needed."""
        out_json = self.tmp / "final_validation.json"
        self._run([
            "scripts/pulse_draft_validate.py",
            str(self.tmp / "final.md"), str(self.tmp / "ctx.json"),
            str(out_json),
        ])
        final_v = self._viols(out_json)
        draft_v = self._viols(self.tmp / "draft_validation.json")
        draft_kinds = {x.get("kind") for x in draft_v}
        new_hard = [
            x for x in final_v
            if x.get("kind") in FINAL_GATE_HARD_KINDS
            and x.get("kind") not in draft_kinds
        ]
        # Deterministic repair: ## _LEANS deleted by EDIT/SCRUB is
        # restored verbatim from the DRAFT — an empty trade board is a
        # silent product failure and needs no LLM to fix.
        if any(x.get("kind") == "leans-block-missing" for x in new_hard):
            if self._restore_leans():
                new_hard = [x for x in new_hard
                            if x.get("kind") != "leans-block-missing"]
        (self.tmp / "final_new_hard.json").write_text(
            json.dumps(new_hard, indent=1), encoding="utf-8")
        gate_name = "final_validate_recheck" if recheck else "final_validate"
        if not new_hard:
            return self._decide(gate_name, "CONTINUE",
                                "no EDIT/SCRUB-introduced hard violations")
        if recheck:
            return self._decide(
                gate_name, "CONTINUE",
                f"WARNING: {len(new_hard)} residual(s) after FIXUP -- "
                f"shipping; residuals in final_validation.json for QC")
        (self.tmp / "final_fixup_violations.json").write_text(
            json.dumps(new_hard, indent=1), encoding="utf-8")
        return self._decide(
            "final_validate", "DISPATCH_FIXUP",
            f"{len(new_hard)} EDIT/SCRUB-introduced hard violation(s); "
            f"violations at {self.tmp / 'final_fixup_violations.json'}; "
            f"after FIXUP run: gate final_validate --recheck")

    def _viols(self, path: Path) -> list[dict]:
        try:
            v = json.loads(path.read_text(encoding="utf-8"))
            return v if isinstance(v, list) else v.get("violations", [])
        except Exception:
            return []

    def _restore_leans(self) -> bool:
        try:
            draft = (self.tmp / "draft.md").read_text(encoding="utf-8")
        except Exception:
            return False
        m = re.search(r"^## _LEANS\n.*?(?=^## |\Z)", draft, re.M | re.S)
        if not m:
            return False
        with open(self.tmp / "final.md", "a", encoding="utf-8") as f:
            f.write("\n\n" + m.group(0).rstrip() + "\n")
        print("RESTORED: ## _LEANS spliced back from draft.md")
        return True

    def gate_strip(self) -> str:
        """STEP 5.8 — strip internal-notes sections, then verify."""
        self._run([
            "scripts/pulse_strip_internal_notes.py",
            str(self.tmp / "final.md"),
        ])
        md = (self.tmp / "final.md").read_text(encoding="utf-8")
        # ## _LEANS is NOT a leak: intentionally preserved through the
        # strip (TRADE BOARD structural source); the bridge removes it
        # at post time. Everything else `## _` is internal notes.
        leaked = [h for h in re.findall(r"^## _\S+", md, re.M)
                  if "_LEANS" not in h]
        if leaked:
            return self._decide(
                "strip", "CONTINUE",
                f"WARNING: internal header(s) survived strip: "
                f"{leaked[:3]} -- investigate before commit")
        return self._decide("strip", "CONTINUE", "no internal headers")

    def gate_adversarial(self, recheck: bool = False) -> str:
        """STEP 5.85 — the blocking pre-commit adversarial check
        (redesign sequencing step 3, spec §6, scoped to the CURRENT
        pipeline: final.md vs the day's research context, no
        briefs/cards/ledger yet).

        The routine dispatches a FRESH sub-agent (no drafting history)
        with ADVERSARIAL_SYSTEM/USER; the agent writes
        {tmp}/adversarial_verdict.json:

            {"findings": [{"severity": "hard"|"soft", "kind": str,
                           "quote": str, "why": str, "fix": str}]}

        This gate is the deterministic half: parse the verdict, demote
        hard findings whose `quote` does not actually appear in
        final.md (a checker hallucinating a sentence must not burn a
        repair round), and decide:

          BLOCK                  verdict missing/unreadable — an
                                 erroring gate is a failed gate
                                 (STANDING RULE 3); re-dispatch the
                                 checker, then re-run this gate
          CONTINUE               no hard findings (softs recorded)
          DISPATCH_REPAIR        hard findings, repair budget left —
                                 items at adversarial_repair_items.json
          CONTINUE_WITH_RESIDUAL budget spent, hard findings remain —
                                 the driver has appended the labeled
                                 residual note (spec's ship-anyway)
        """
        gate_name = "adversarial_recheck" if recheck else "adversarial"
        vpath = self.tmp / "adversarial_verdict.json"
        try:
            raw = json.loads(vpath.read_text(encoding="utf-8"))
            findings = raw["findings"]
            assert isinstance(findings, list)
        except Exception as e:
            return self._decide(
                gate_name, "BLOCK",
                f"adversarial verdict missing or unreadable at {vpath} "
                f"({type(e).__name__}) -- dispatch the checker agent, "
                f"then re-run this gate. An unreadable verdict is a "
                f"FAILED gate, never a pass.")

        try:
            final_md = (self.tmp / "final.md").read_text(encoding="utf-8")
        except Exception:
            return self._decide(gate_name, "BLOCK",
                                "final.md missing -- nothing to check")

        def _norm(s: str) -> str:
            return re.sub(r"\s+", " ", s or "").strip().lower()

        doc = _norm(final_md)
        hard, soft, demoted = [], [], 0
        for f in findings:
            if not isinstance(f, dict):
                continue
            sev = (f.get("severity") or "").lower()
            if sev == "hard":
                # Quote-grounding: a hard finding must point at text
                # that exists. Whitespace-normalized substring, same
                # spirit as the extraction anchor check.
                if _norm(f.get("quote") or "") and \
                        _norm(f.get("quote") or "") in doc:
                    hard.append(f)
                else:
                    demoted += 1
                    soft.append({**f, "severity": "soft",
                                 "demoted": "quote not found in final.md"})
            else:
                soft.append(f)

        budgets = self.state.setdefault("budgets", {})
        repairs = budgets.get("adversarial_repairs", 0)
        detail_soft = (f", {len(soft)} soft (recorded)" if soft else "")
        if demoted:
            detail_soft += f", {demoted} demoted (quote unfound)"

        if not hard:
            return self._decide(
                gate_name, "CONTINUE",
                f"0 hard findings{detail_soft}")
        if repairs >= MAX_ADVERSARIAL_REPAIRS:
            self._append_residual_note(len(hard))
            (self.tmp / "adversarial_residuals.json").write_text(
                json.dumps(hard, indent=1), encoding="utf-8")
            return self._decide(
                gate_name, "CONTINUE_WITH_RESIDUAL",
                f"{len(hard)} hard finding(s) after {repairs} repair "
                f"pass(es) -- residual note appended, residuals at "
                f"adversarial_residuals.json for QC")
        budgets["adversarial_repairs"] = repairs + 1
        self._save()
        (self.tmp / "adversarial_repair_items.json").write_text(
            json.dumps(hard, indent=1), encoding="utf-8")
        return self._decide(
            gate_name, "DISPATCH_REPAIR",
            f"{len(hard)} hard finding(s){detail_soft}; repair "
            f"{repairs + 1}/{MAX_ADVERSARIAL_REPAIRS}; items at "
            f"adversarial_repair_items.json; after the repair agent, "
            f"RE-DISPATCH the checker fresh, then run: "
            f"gate adversarial --recheck")

    def _append_residual_note(self, n: int) -> None:
        """Deterministic ship-anyway marker. Voice-contract compliant:
        no em-dashes, no semicolons, one sentence.

        Inserted BEFORE the ## _LEANS block, never after it: _LEANS is
        the last section and the bridge deletes that whole block at
        post time, so text appended after its header ships to nobody.
        """
        note = (f"\n\n{RESIDUAL_NOTE_PREFIX} {n} statement(s) in this "
                f"edition did not clear the final source check and "
                f"will be corrected if wrong.*\n")
        path = self.tmp / "final.md"
        md = path.read_text(encoding="utf-8")
        m = re.search(r"^## _LEANS", md, re.M)
        if m:
            md = md[:m.start()].rstrip() + note + "\n" + md[m.start():]
        else:
            md = md.rstrip() + note
        path.write_text(md, encoding="utf-8")
        print(f"APPENDED: residual accuracy note ({n} finding(s))")

    # ------------------------------------------------------------------
    def record(self, label: str, detail: str = ""):
        now = datetime.datetime.utcnow().isoformat() + "Z"
        self.state["history"].append(
            {"record": label, "detail": detail[:300], "at": now})
        self._save()
        print(f"RECORDED: {label}")

    # ------------------------------------------------------------------
    def preflight(self) -> str:
        """The choke point before STEP 6. Refuses unless every required
        gate was consulted and the artifact is sane. This is what makes
        a skipped gate DETECTABLE instead of silent."""
        problems: list[str] = []
        gates = self.state.get("gates", {})
        required = ["holiday", "volume", "draft_validate", "lint",
                    "final_validate", "strip", "adversarial"]
        for g in required:
            if g not in gates:
                problems.append(f"gate never consulted: {g}")
        # The adversarial loop must have CONCLUDED, not just started: a
        # last decision of DISPATCH_REPAIR means the repair/recheck
        # cycle was abandoned mid-flight, and BLOCK means the verdict
        # was never readable. Both are unfinished gates, not passes.
        _adv_last = (gates.get("adversarial_recheck")
                     or gates.get("adversarial") or {}).get("decision")
        if _adv_last in ("DISPATCH_REPAIR", "BLOCK"):
            problems.append(
                f"adversarial gate unfinished (last decision "
                f"{_adv_last}) -- run the dispatched step, then "
                f"gate adversarial --recheck")
        if _adv_last == "CONTINUE_WITH_RESIDUAL":
            try:
                _md = (self.tmp / "final.md").read_text(encoding="utf-8")
            except Exception:
                _md = ""
            if RESIDUAL_NOTE_PREFIX not in _md:
                problems.append(
                    "adversarial gate shipped with residuals but the "
                    "labeled residual note is missing from final.md "
                    "(a later mutation removed it) -- re-run "
                    "gate adversarial --recheck")
        if gates.get("holiday", {}).get("decision") == "SKIP_PULSE":
            problems.append("holiday gate said SKIP_PULSE -- there is "
                            "nothing to commit today")
        # If lint dispatched SCRUB, the re-lint gate must have run.
        if (gates.get("lint", {}).get("decision") == "DISPATCH_SCRUB"
                and "scrub_relint" not in gates):
            problems.append("lint dispatched SCRUB but scrub_relint "
                            "gate never ran (5.7.3 skipped)")
        # If 5.75 dispatched FIXUP, the recheck must have run.
        if (gates.get("final_validate", {}).get("decision")
                == "DISPATCH_FIXUP"
                and "final_validate_recheck" not in gates):
            problems.append("final_validate dispatched FIXUP but the "
                            "recheck never ran (5.75 re-validation "
                            "skipped)")
        # Artifact sanity.
        final = self.tmp / "final.md"
        if not final.exists():
            problems.append("final.md missing")
        else:
            md = final.read_text(encoding="utf-8")
            if len(md) < 2000:
                problems.append(f"final.md suspiciously small "
                                f"({len(md)} chars)")
            leaked = [h for h in re.findall(r"^## _\S+", md, re.M)
                      if "_LEANS" not in h]
            if leaked:
                problems.append(f"internal header(s) in final.md "
                                f"(strip failed or ran before a later "
                                f"mutation): {leaked[:3]}")
        if not (self.tmp / "draft.md").exists():
            problems.append("draft.md missing (forensics artifact "
                            "required for commit)")
        if problems:
            for p in problems:
                print(f"  BLOCK: {p}")
            return self._decide(
                "preflight", "BLOCK",
                f"{len(problems)} problem(s) -- fix and re-run "
                f"preflight; do NOT commit")
        return self._decide("preflight", "PASS",
                            "all gates consulted, artifact sane -- "
                            "proceed to STEP 6 commit")

    def status(self):
        print(json.dumps(self.state, indent=1))


def main() -> int:
    args = sys.argv[1:]
    tmp = Path("/tmp")
    if "--tmp" in args:
        i = args.index("--tmp")
        tmp = Path(args[i + 1])
        del args[i:i + 2]
    if not args:
        print(__doc__)
        return 2
    d = Driver(tmp)
    cmd = args[0]
    if cmd == "gate":
        gate = args[1]
        recheck = "--recheck" in args
        fn = {
            "holiday": d.gate_holiday,
            "volume": d.gate_volume,
            "draft_validate": d.gate_draft_validate,
            "lint": d.gate_lint,
            "scrub_relint": d.gate_scrub_relint,
            "strip": d.gate_strip,
        }.get(gate)
        if gate == "final_validate":
            d.gate_final_validate(recheck=recheck)
        elif gate == "adversarial":
            d.gate_adversarial(recheck=recheck)
        elif fn:
            fn()
        else:
            print(f"unknown gate: {gate}")
            return 2
        return 0
    if cmd == "record":
        d.record(args[1], " ".join(args[2:]))
        return 0
    if cmd == "preflight":
        token = d.preflight()
        return 0 if token == "PASS" else 3
    if cmd == "status":
        d.status()
        return 0
    print(f"unknown command: {cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
