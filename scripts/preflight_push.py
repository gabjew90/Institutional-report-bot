#!/usr/bin/env python3
"""Pre-push gate. REQUIRED before any push to the deploy branch.

WHY
===
A push to this branch auto-redeploys the live Discord bot. On
2026-08-26 a push took the worker down for seven minutes: a regex with
a mid-pattern `(?i)` compiled with a DeprecationWarning on the local
Python 3.10 and raised `re.error` on the container's Python 3.12, so
`bot.py` died on import and crash-looped.

That was the NINTH divergence between this machine and production, after
eight config ones. The first eight were about what gets SENT; this one is
about what the code RUNS ON. Same class, same failure mode: the local
process is not the deployed process, nothing says so, and the difference
only surfaces in production.

WHAT THIS CHECKS
================
1. Local Python major.minor == the container's. A mismatch is a FAILURE,
   not a warning. If this machine cannot run the deployed interpreter, it
   cannot validate a deploy from here — say so rather than guess.
2. `discord_bot.bot` imports with DeprecationWarnings promoted to
   errors. Catches the whole class of "deprecated here, removed there"
   before the deploy instead of after.
3. The diet smoke's own checks are reachable and terminate.

CHECK-THE-CHECKS
================
A gate that ERRORS is a gate that FAILED. A check raising an unexpected
exception is not "inconclusive" — it is a failure, and it exits non-zero
like any other. Treating an erroring check as a pass is how a broken
guard reads green: this file's own smoke guard shipped with a syntax
error on 2026-08-26 and could not run at all.

USAGE
=====
    python scripts/preflight_push.py
    exit 0 -> safe to push.  non-zero -> do not push.
"""
from __future__ import annotations

import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The interpreter the Railway container runs. Read off a traceback:
#   File "/root/.nix-profile/lib/python3.12/asyncio/base_events.py"
# Update this ONLY when the container's runtime actually changes.
CONTAINER_PYTHON = (3, 12)

_results: list[tuple[str, bool, str]] = []


def _record(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'} {name}")
    if detail and not ok:
        for line in detail.splitlines()[:14]:
            print("       " + line)


def check_python_parity() -> None:
    local = sys.version_info[:2]
    if local == CONTAINER_PYTHON:
        _record(f"python parity — local {local[0]}.{local[1]} == "
                f"container {CONTAINER_PYTHON[0]}.{CONTAINER_PYTHON[1]}",
                True)
        return
    _record(
        f"python parity — local {local[0]}.{local[1]} != container "
        f"{CONTAINER_PYTHON[0]}.{CONTAINER_PYTHON[1]}",
        False,
        "This machine cannot validate a deploy: syntax and stdlib\n"
        "behaviour differ between these versions, and the difference\n"
        "only shows up after the push.\n"
        "\n"
        f"Install Python {CONTAINER_PYTHON[0]}.{CONTAINER_PYTHON[1]} and\n"
        "re-run this gate with it:\n"
        f"    winget install Python.Python.{CONTAINER_PYTHON[0]}."
        f"{CONTAINER_PYTHON[1]}\n"
        f"    py -{CONTAINER_PYTHON[0]}.{CONTAINER_PYTHON[1]} "
        "scripts/preflight_push.py\n"
        "\n"
        "There is deliberately NO override flag. An override is how a\n"
        "known divergence becomes a permanent one.",
    )


def check_import_under_error_warnings() -> None:
    """bot.py must import with deprecations promoted to errors."""
    r = subprocess.run(
        [sys.executable, "-W", "error::DeprecationWarning", "-c",
         "from discord_bot.bot import create_bot"],
        capture_output=True, text=True, cwd=REPO,
    )
    _record("bot.py imports with deprecations as errors",
            r.returncode == 0,
            (r.stderr or "").strip())


def check_gates_are_runnable() -> None:
    """The diet smoke must RUN. Syntax errors in a gate are failures."""
    r = subprocess.run(
        [sys.executable, os.path.join(REPO, "scripts",
                                      "smoke_ask_prompt_diet.py")],
        capture_output=True, text=True, cwd=REPO,
    )
    out = (r.stdout or "") + (r.stderr or "")
    # returncode 1 is a legitimate FAIL verdict from the gate itself.
    # A traceback means the gate could not render a verdict at all,
    # which is worse than failing.
    broke = ("Traceback" in out or "SyntaxError" in out
             or "CHECKS FAILED" not in out and "TESTS PASS" not in out)
    _record("diet smoke is runnable (renders a verdict, not a traceback)",
            not broke,
            "\n".join(out.strip().splitlines()[-12:]))


def main() -> int:
    print("=== pre-push gate ===")
    print(f"repo: {REPO}")
    print()
    for fn in (check_python_parity,
               check_import_under_error_warnings,
               check_gates_are_runnable):
        try:
            fn()
        except Exception as e:                    # check-the-checks
            _record(fn.__name__, False,
                    f"the check itself raised {type(e).__name__}: {e}\n"
                    f"An erroring gate is a FAILED gate, never a pass.")
    failed = [n for n, ok, _ in _results if not ok]
    print()
    if failed:
        print(f"{len(failed)} of {len(_results)} gates failed — DO NOT PUSH")
        return 1
    print(f"all {len(_results)} gates passed — safe to push")
    return 0


if __name__ == "__main__":
    sys.exit(main())
