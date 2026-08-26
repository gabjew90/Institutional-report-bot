"""Smoke test: /ask prompt diet — behavior anchors survive, bloat doesn't.

Context (2026-07-27 prompt review): the system prompt had grown to
~95K chars (~24K tokens) by incident accretion — every dated failure
got a narrative paragraph. The 07-08 diagnosis in bot.py already tied
prompt weight to grounding skips ("the model answers from that context
and its priors instead"). The diet keeps every RULE and moves the
incident stories to code comments.

Two guards:
  1. CONCEPT ANCHORS — one distinctive substring per rule family that
     must survive the diet (complements smoke_ask_prompt_contract.py,
     which freezes the 25 data-correctness anchors verbatim).
  2. SIZE CEILING — the prompt must stay under the ceiling so it can't
     silently re-bloat one incident paragraph at a time. New rules are
     fine; they must displace narrative, not stack on it.
"""

import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


# Provisional budget, owner-set, not a measured optimum. Ratchet:
# decreases only. Raising requires owner approval plus ask_fixture_run
# evidence.
_SIZE_CEILING = 63_590  # chars. Was 65,000; 94,795 pre-diet.

_FIXTURE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tests", "ask_fixtures")
_RUNNER = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "ask_fixture_run.py")
_VALIDATOR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "ask_response_validate.py")
_DATE_RE = re.compile(r"20\d\d-\d\d-\d\d")

# (anchor substring, rule family it proves survived)
_CONCEPT_ANCHORS = [
    # framing
    ("ghost writer", "ghost-writer framing (filter-critical — see memory)"),
    ("options-alert service", "customer-respect framing"),
    # Rule moved to code 2026-08-26 (scripts/ask_response_validate.py,
    # check_meta_plumbing). The prompt keeps one line naming the
    # behavior; the anchor tracks that line, not the deleted block.
    ("narrate the bot's own plumbing", "no plumbing talk (enforced "
     "by scripts/ask_response_validate.py)"),
    # types + format
    ("TYPE 1", "type taxonomy"),
    ("TYPE 2", "type taxonomy"),
    ("TYPE 3", "type taxonomy"),
    ("Quick read", "depth tiers"),
    ("Full DD", "depth tiers"),
    ("→", "literal arrow-bullet format"),
    ("No emojis", "emoji ban"),
    ("THE VOICE", "voice register spec"),
    ("default down", "type-3 trigger discipline"),
    ("personal color beats P&L", "roast material hierarchy"),
    ("fresh material", "anti-recycling"),
    ("Closure messages get closure replies", "closure handling"),
    # secrecy + presentation
    ("Never narrate them", "instruction secrecy"),
    ("Don't acknowledge being a bot", "no bot self-reference"),
    ("No apologizing", "no apologies"),
    ("NEVER cite your context blocks", "no context-block citations"),
    # data-adjacent behavior
    ("Company filings", "source-quality hierarchy"),
    ("Recency is its own trigger", "recency search trigger"),
    ("no clean consensus", "uncertainty handling"),
    ("Bad-faith framing", "bad-faith questions still get answers"),
    # 2026-07-30: "@omniwiz how much did citadel make today" came back
    # as a Type 2 banter paragraph instead of Type 1 arrows. Citadel
    # doesn't publish daily P&L, so the question matched Type 2's old
    # trigger wording ("no clean factual answer") literally. Undisclosed
    # != subjective.
    ("Undisclosed isn't subjective", "unavailable data stays Type 1"),
    # 2026-07-30: "who are the happiest people in the chat? How about
    # the angriest" came back as a one-line jab at the asker with no
    # names and no data behind it.
    ("Room-superlative questions are Type 1", "room superlatives get names"),
    ("binds on EVERY type", "personal-over-P&L applies to banter too"),
    ("QUOTATION", "verbatim quotation rule"),
    ("CHART COMMANDS", "fc-command lexicon"),
    ("Match by username", "username-keyed attribution"),
    ("top 5", "leaderboard cap"),
    ("how do I climb", "rank-climb exception"),
    ("Multi-position list format", "caller book format"),
    ("PRIORITY ORDER", "conflict priority list"),
]


def test_concept_anchors_present():
    import discord_bot.bot as bot_mod
    ins = bot_mod._ASK_SYSTEM_INSTRUCTION
    missing = [(a, n) for a, n in _CONCEPT_ANCHORS if a not in ins]
    if missing:
        lines = "\n".join(f"  - {n}   (anchor: {a!r})" for a, n in missing)
        _fail(f"{len(missing)} rule famil(ies) dropped by the diet:\n{lines}")
    _ok(f"all {len(_CONCEPT_ANCHORS)} concept anchors present")


def test_size_ceiling():
    import discord_bot.bot as bot_mod
    n = len(bot_mod._ASK_SYSTEM_INSTRUCTION)
    if n > _SIZE_CEILING:
        _fail(
            f"/ask system prompt is {n} chars (ceiling {_SIZE_CEILING}, "
            f"over by {n - _SIZE_CEILING}). New rules must displace "
            f"narrative, not stack on it — move incident stories to the "
            f"INCIDENT LEDGER in the ask_prompt.py module docstring. "
            f"The ceiling ratchets down only; raising it is owner-only "
            f"and needs ask_fixture_run evidence.")
    _ok(f"prompt size {n} chars <= {_SIZE_CEILING} ceiling")


def test_no_incident_dates_in_prompt():
    """Incident dates belong in the docstring ledger, not the prompt body.

    A date in the prompt is the signature of narrative accretion: the rule
    gets restated with its story attached, and the story is what made the
    prompt 94,795 chars. The RULE is what the model needs; the date is
    provenance for humans and belongs in the ledger.
    """
    import discord_bot.bot as bot_mod
    ins = bot_mod._ASK_SYSTEM_INSTRUCTION
    hits = []
    for i, line in enumerate(ins.splitlines(), 1):
        for m in _DATE_RE.finditer(line):
            hits.append((i, m.group(0), line.strip()[:96]))
    if hits:
        lines = "\n".join(f"  line {i}: {d}  |  {t}" for i, d, t in hits)
        _fail(
            f"{len(hits)} incident date(s) in the prompt body — move the "
            f"provenance to the INCIDENT LEDGER in the ask_prompt.py "
            f"module docstring and leave the rule:\n{lines}")
    _ok("no incident dates in the prompt body")


def test_every_incident_has_a_fixture():
    """A new incident must arrive with a fixture, not just a rule.

    A rule with no fixture cannot be shown to work and cannot be safely
    deleted later, which is exactly how the prompt became append-only.
    """
    import discord_bot.ask_prompt as ap
    doc = ap.__doc__ or ""
    ledger = re.findall(r"^\s{2}(20\d\d-\d\d-\d\d)", doc, re.M)
    n_fix = len([f for f in os.listdir(_FIXTURE_DIR) if f.endswith(".json")])
    if n_fix < len(ledger):
        _fail(
            f"{n_fix} fixtures for {len(ledger)} INCIDENT LEDGER entries. "
            f"Every incident that earned a rule needs a fixture proving "
            f"the rule works, or the rule can never be safely deleted.")
    _ok(f"{n_fix} fixtures >= {len(ledger)} ledger entries")


def test_fixture_assertions_self_test():
    """The fixtures must be able to tell a good answer from a bad one.

    A fixture whose assertions pass both is not evidence, it is
    decoration, and it reads as a green build while detecting nothing.
    """
    r = subprocess.run([sys.executable, _RUNNER, "--self-test"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        tail = "\n".join((r.stdout or "").strip().splitlines()[-25:])
        _fail("ask_fixture_run.py --self-test failed (TOO WEAK / BROKEN / "
              f"MISSING fixtures block the build):\n{tail}")
    summary = [ln for ln in (r.stdout or "").splitlines()
               if ln.startswith("OK ")]
    _ok(f"fixture self-test clean — {summary[-1] if summary else 'exit 0'}")


def test_response_validator():
    """Rules deleted from the prompt must still be enforced somewhere.

    NEVER META-NARRATE moved to ask_response_validate.check_meta_plumbing
    on 2026-08-26. If that validator stops catching the recorded
    violations, the rule is enforced NOWHERE — the prompt text is already
    gone. This check is what makes the deletion safe.
    """
    r = subprocess.run([sys.executable, _VALIDATOR, "--self-test"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        tail = "\n".join((r.stdout or "").strip().splitlines()[-20:])
        _fail("ask_response_validate --self-test failed. A rule deleted "
              "from the prompt is now enforced by nothing:\n" + tail)
    _ok("response validator catches every recorded violation")


def test_production_import_is_clean():
    """bot.py must import with DeprecationWarnings promoted to errors.

    On 2026-08-26 a mid-pattern `(?i)` in ask_response_validate compiled
    with a DeprecationWarning on local Python 3.10 and raised re.error on
    the container's Python 3.12, crash-looping the worker on import. The
    warning was visible in harness output for hours and nobody read it.

    Promoting warnings to errors here catches the whole class -- any
    construct Python is deprecating locally but has already removed in a
    newer runtime -- BEFORE it reaches Railway.
    """
    r = subprocess.run(
        [sys.executable, "-W", "error::DeprecationWarning", "-c",
         "from discord_bot.bot import create_bot"],
        capture_output=True, text=True,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if r.returncode != 0:
        tail = "
".join((r.stderr or "").strip().splitlines()[-12:])
        _fail("bot.py does not import cleanly with deprecations as "
              "errors. This is what takes the worker down on deploy:
"
              + tail)
    _ok("bot.py imports clean with deprecations promoted to errors")


# Every check runs even after one fails. Fixing a gate one rediscovered
# failure at a time is how a build stays red for a week.
_CHECKS = [
    test_concept_anchors_present,
    test_size_ceiling,
    test_no_incident_dates_in_prompt,
    test_every_incident_has_a_fixture,
    test_fixture_assertions_self_test,
    test_response_validator,
    test_production_import_is_clean,
]


if __name__ == "__main__":
    print("=== /ask prompt diet smoke ===")
    failed = 0
    for check in _CHECKS:
        try:
            check()
        except SystemExit:
            failed += 1
        except Exception as e:            # a broken check is a failed check
            print(f"FAIL {check.__name__} raised {type(e).__name__}: {e}")
            failed += 1
    if failed:
        print(f"\n{failed} of {len(_CHECKS)} /ASK PROMPT DIET CHECKS FAILED")
        sys.exit(1)
    print("\nALL /ASK PROMPT DIET SMOKE TESTS PASS")
