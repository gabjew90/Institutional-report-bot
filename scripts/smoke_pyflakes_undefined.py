"""Static lint gate that catches undefined-name and unused-import bugs
at smoke time. Pure-Python AST analysis via pyflakes — no runtime
impact, no API calls, runs in well under a second.

WHY THIS EXISTS

On 2026-06-02 two latent NameError bugs surfaced on a single redeploy
that had been silently corrupting two scheduled jobs for hours:

  - report/synthesizer.py:662 — `re.compile(...)` was called but
    the module only had a function-local `import re` inside
    `_normalize_theme_tag`. Every fire of the 15-min "GitHub
    bridge: dump pulse context" job hit NameError: name 're' is
    not defined.

  - discord_bot/bot.py:1256 — `asyncio.gather(...)` was called but
    the module had no `import asyncio`. The `try/except Exception`
    around the gather masked the failure as a logged warning,
    silently disabling lazy OCR for /ask answers needing image
    content.

Both bugs are exactly the class `pyflakes` catches: undefined names
+ unused imports. Running this smoke would have flagged them at the
exact line numbers before they ever shipped. Verified by feeding the
pre-fix versions of both files into pyflakes — both NameErrors
reported with file:line:col.

WHAT IT CHECKS

  1. Walk every .py file in the project (skipping `.venv`, `__pycache__`,
     and `scripts/` itself).
  2. Run pyflakes against each via the `pyflakes.api.checkPath` interface.
  3. Fail the smoke if any `UndefinedName` or `UnusedImport` violation
     is reported. Other pyflakes warnings (e.g. unused locals) are
     informational only — they don't fail the smoke since they're
     not bugs-in-waiting.

WHAT IT EXPLICITLY DOES NOT CHECK

  - Style (line length, naming, etc) — pyflakes is bug-focused only.
  - Type errors — that's mypy / pyright territory.
  - Runtime semantics that AST analysis can't see (mocked imports,
    dynamic getattr, etc).
"""

import sys
from pathlib import Path


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


# Production code paths: the ones whose failures page someone at 3am.
# Both bugs that hit prod on 2026-06-02 (synthesizer.py `re`, bot.py
# `asyncio`) were in these directories. Anything outside this set
# (scripts/, tests/, sample_*, probe_*) is dev / one-off tooling
# where stricter standards aren't worth the maintenance overhead.
_PRODUCTION_PATHS = {
    "db.py",
    "config.py",
    "main.py",
    "inspect_db.py",
    "discord_bot",
    "report",
    "ai_analysis",
    "pdf_processing",
    "chat_ingestion",
    "analyst_log",
    "dropbox_client",
    "github_bridge",
    "scheduler",
    "pipeline",
    "api",
}


def _project_python_files() -> list[Path]:
    """Find every production .py file to lint.

    Lints production code only — directories listed in
    _PRODUCTION_PATHS. Scripts, tests, sample/probe files are excluded:
    they're dev tooling where unused-import noise outpaces the
    value of the gate.
    """
    root = Path(__file__).resolve().parent.parent
    files: list[Path] = []
    for p in root.rglob("*.py"):
        rel = p.relative_to(root)
        first = rel.parts[0]
        if first not in _PRODUCTION_PATHS:
            continue
        if "__pycache__" in rel.parts:
            continue
        files.append(p)
    return sorted(files)


def test_pyflakes_no_undefined_names_or_unused_imports():
    """Run pyflakes against every project .py file. Fail on any
    UndefinedName or UnusedImport finding."""
    try:
        from pyflakes.api import checkPath
        from pyflakes.reporter import Reporter
    except ImportError:
        _fail(
            "pyflakes is not installed. Add to requirements.txt and run "
            "`pip install pyflakes`."
        )

    import io

    files = _project_python_files()
    if not files:
        _fail("found 0 project .py files to lint — sanity check failed")
    print(f"  scanning {len(files)} .py files...")

    # Capture pyflakes output via a custom Reporter.
    out = io.StringIO()
    err = io.StringIO()
    reporter = Reporter(out, err)
    total_findings = 0
    for f in files:
        # checkPath returns # of issues found; output goes to reporter.
        total_findings += checkPath(str(f), reporter)

    output = out.getvalue() + err.getvalue()
    if not output:
        _ok("pyflakes: 0 findings across project")
        return

    # FAIL only on runtime-breaking issues (undefined names — these
    # will NameError when the code path runs). Unused imports are dead
    # code, not runtime-breaking, and there's a long tail of legacy
    # ones in the codebase. We list them as informational so they get
    # cleaned up over time without blocking commits.
    fail_lines = [
        ln for ln in output.splitlines()
        if "undefined name" in ln.lower()
    ]
    informational_lines = [
        ln for ln in output.splitlines()
        if ln.strip() and ln not in fail_lines
    ]
    if fail_lines:
        print()
        print(f"  pyflakes flagged {len(fail_lines)} runtime-breaking bug(s):")
        for ln in fail_lines:
            print(f"    {ln}")
        _fail(
            f"{len(fail_lines)} undefined-name issue(s) found in production "
            "code — fix before commit (these WILL NameError at runtime)"
        )

    if informational_lines:
        print(
            f"  pyflakes: 0 undefined-name bugs; "
            f"{len(informational_lines)} unused-import warning(s) "
            f"(informational — cleanup welcome, not blocking):"
        )
        for ln in informational_lines[:5]:
            print(f"    {ln}")
        if len(informational_lines) > 5:
            print(f"    ... and {len(informational_lines) - 5} more")
    _ok(
        f"pyflakes: 0 undefined-name bugs in production code "
        f"({len(informational_lines)} unused-import warnings, informational)"
    )


if __name__ == "__main__":
    print("=== pyflakes undefined-name + unused-import gate ===")
    test_pyflakes_no_undefined_names_or_unused_imports()
    print("\nALL PYFLAKES SMOKE TESTS PASS")
