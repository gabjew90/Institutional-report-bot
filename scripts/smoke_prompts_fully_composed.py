"""Smoke: no prompt reaches a model with an unresolved placeholder.

2026-08-11. STEP 1 of the synthesis routine read the prompt constants by
`cat ai_analysis/prompts.py` and lifting the triple-quoted strings. That
is the SOURCE text. Three prompts are only finished at Python import
time and carry `<<PLACEHOLDER>>` tokens in the file:

    SCRUB_SYSTEM  <<SCRUB_REFERENCE_BLOCK>>
    DRAFT_SYSTEM  <<VOICE_RULES_BLOCK>>
    AUDIT_SYSTEM  <<VOICE_RULES_BLOCK>>

A raw-file reader gets the literal token and silently loses the
banned-phrase list, the jargon map and the rewrite-over-gloss rule. The
SCRUB placeholder predates this; the two voice ones were added the same
day the voice contract was wired, and would have reproduced the exact
bug that wiring was meant to fix.

scripts/dump_prompts.py composes the prompts and fails loudly on any
leftover token. These tests pin both halves: the composed prompts are
clean, and the routine tells the operator to use the script.
"""

import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLACEHOLDER_RE = re.compile(r"<<[A-Z_]+>>")


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_no_composed_prompt_has_a_placeholder():
    import ai_analysis.prompts as p
    from scripts.dump_prompts import PROMPT_NAMES
    bad = []
    for name in PROMPT_NAMES:
        text = getattr(p, name, None)
        if not isinstance(text, str):
            _fail(f"prompts.py is missing {name}")
        found = PLACEHOLDER_RE.findall(text)
        if found:
            bad.append(f"{name}: {sorted(set(found))}")
    if bad:
        _fail("composed prompts still contain placeholders:\n  "
              + "\n  ".join(bad))
    _ok(f"all {len(PROMPT_NAMES)} composed prompts are placeholder-free")


def test_source_file_does_have_placeholders():
    """Guards the premise. If the source stops carrying placeholders the
    dump script is no longer load-bearing and this suite should be
    revisited rather than quietly passing forever."""
    src = open(os.path.join(REPO, "ai_analysis", "prompts.py"),
               encoding="utf-8").read()
    if not PLACEHOLDER_RE.search(src):
        _fail("prompts.py source has no placeholders — the raw-read hazard "
              "this suite guards may no longer exist; re-derive before "
              "deleting anything")
    _ok("source file still carries placeholders, so composition matters")


def test_dump_script_writes_clean_prompts():
    from scripts.dump_prompts import PROMPT_NAMES
    with tempfile.TemporaryDirectory() as td:
        r = subprocess.run(
            [sys.executable, os.path.join(REPO, "scripts", "dump_prompts.py"), td],
            capture_output=True, text=True, cwd=REPO,
            env={**os.environ, "PYTHONPATH": REPO, "PYTHONIOENCODING": "utf-8"},
        )
        if r.returncode != 0:
            _fail(f"dump_prompts.py exited {r.returncode}: {r.stderr[:400]}")
        for name in PROMPT_NAMES:
            path = os.path.join(td, f"{name}.txt")
            if not os.path.exists(path):
                _fail(f"{name}.txt was not written")
            if PLACEHOLDER_RE.search(open(path, encoding="utf-8").read()):
                _fail(f"{name}.txt contains an unresolved placeholder")
    _ok("dump_prompts.py writes all prompts with no placeholders")


def test_dump_script_fails_loudly_on_a_placeholder():
    """The point of the script is the non-zero exit. Prove it fires."""
    with tempfile.TemporaryDirectory() as td:
        stub = os.path.join(td, "fake_prompts.py")
        harness = os.path.join(td, "run.py")
        open(stub, "w", encoding="utf-8").write(
            'DRAFT_SYSTEM = "voice: <<VOICE_RULES_BLOCK>>"\n')
        open(harness, "w", encoding="utf-8").write(
            "import sys, types\n"
            f"sys.path.insert(0, {REPO!r})\n"
            "import scripts.dump_prompts as d\n"
            "mod = types.ModuleType('ai_analysis.prompts')\n"
            "mod.DRAFT_SYSTEM = 'voice: <<VOICE_RULES_BLOCK>>'\n"
            "sys.modules['ai_analysis.prompts'] = mod\n"
            "d.PROMPT_NAMES = ('DRAFT_SYSTEM',)\n"
            "sys.exit(d.main(['dump', sys.argv[1]]))\n")
        r = subprocess.run(
            [sys.executable, harness, os.path.join(td, "out")],
            capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": REPO, "PYTHONIOENCODING": "utf-8"},
        )
        if r.returncode == 0:
            _fail("dump_prompts.py exited 0 on a prompt with an unresolved "
                  "placeholder — the guard does not actually guard")
    _ok("dump_prompts.py exits non-zero when a placeholder survives")


def test_routine_step1_uses_the_script():
    path = os.path.join(REPO, "docs", "superpowers", "routines",
                        "synthesis-routine.md")
    md = open(path, encoding="utf-8").read()
    step1 = md[md.find("## STEP 1"):md.find("## STEP 2")]
    if "dump_prompts.py" not in step1:
        _fail("STEP 1 does not run dump_prompts.py — the routine will read "
              "raw source and lose the composed blocks")
    if "Do NOT `cat ai_analysis/prompts.py`" not in step1:
        _fail("STEP 1 does not warn against reading the raw source")
    _ok("routine STEP 1 reads composed prompts, not raw source")


if __name__ == "__main__":
    test_no_composed_prompt_has_a_placeholder()
    test_source_file_does_have_placeholders()
    test_dump_script_writes_clean_prompts()
    test_dump_script_fails_loudly_on_a_placeholder()
    test_routine_step1_uses_the_script()
    print("\nAll prompt-composition smoke tests passed.")
