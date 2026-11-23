"""The bridge's own check that a pending pulse is the text the routine's
checks passed (audit fix 4, 2026-11-23).

Until now the bridge posted whatever reached `pulse-output/pending/`. The
routine's gates were instructions to the model: a BLOCK told it not to
commit, and nothing stopped it. The gate trail on the branch could not be
used as evidence either, because the model types it out when it pushes
(see scripts/pulse_seal.py). This reads the seal the driver's `compose`
step writes into the pulse file and recomputes it.

`PULSE_GATE_MODE` on the worker (config.pulse_gate_mode):
  record   the default. A failed check pages ops and writes a marker
           beside the QC review. The pulse is still posted. This is the
           measuring period: how often the model's transcription changes
           the text has never been measured, and a refusal costs the
           routine's pulse (the 10:45 ET backup pulse posts instead).
  enforce  a failed check moves the pulse to delivery-failed/ and it is
           not posted. The owner's switch, once record mode has shown the
           check does not fire on faithful days.
  off      no check.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

MODES = ("record", "enforce", "off")


def mode() -> str:
    try:
        from config import settings
        m = (getattr(settings, "pulse_gate_mode", "") or "record").strip().lower()
    except Exception:
        m = "record"
    return m if m in MODES else "record"


def check(raw_markdown: str) -> list[str]:
    """Why this pending file is not the checked text; empty when it is."""
    from scripts import pulse_seal
    p = pulse_seal.problem(raw_markdown or "")
    return [p] if p else []


def verdict(raw_markdown: str) -> tuple[str, list[str]]:
    """("post" | "refuse", problems). Never raises: a fault in the check
    itself posts the pulse and reports the fault as the problem."""
    m = mode()
    if m == "off":
        return "post", []
    try:
        problems = check(raw_markdown)
    except Exception as e:                                       # noqa: BLE001
        log.error(f"Bridge: gate check failed to run: {e}", exc_info=True)
        return "post", [f"the gate check itself failed: {type(e).__name__}: {e}"]
    if problems and m == "enforce":
        return "refuse", problems
    return "post", problems


def marker(base: str, problems: list[str], action: str) -> str:
    """The sidecar written to qc-reviews/<base>.gate.md."""
    lines = [f"# Gate check — {base}", "",
             f"## Status: {'REFUSED' if action == 'refuse' else 'POSTED WITH A FAILED GATE CHECK'} (bridge)",
             "", f"- **Mode:** {mode()}", ""]
    lines += [f"- {p}" for p in problems]
    lines += ["",
              "The seal in a pulse's frontmatter is written by `scripts/pulse_driver.py compose`, "
              "in code, only after a preflight PASS and only from the text preflight saw. "
              "The bridge recomputes it over the file that reached `pulse-output/pending/`.",
              ""]
    if action == "refuse":
        lines += ["The pulse was moved to `pulse-output/delivery-failed/` and not posted. "
                  "The backup pulse posts at 10:45 ET when no scheduled pulse has. "
                  "To post this file anyway, set `PULSE_GATE_MODE=record` on the worker and move "
                  "it back to `pulse-output/pending/`.", ""]
    return "\n".join(lines)
