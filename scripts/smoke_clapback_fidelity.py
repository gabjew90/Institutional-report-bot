"""Smoke: clapback fidelity guard + cache-friendly prompt ordering.

Context (2026-07-29 roast-thread incident): in a multi-party thread
the bot attributed ZHawk's receipts to bearishkyle — "overnight XSP
750 calls" (ZHawk's documented trade), a "startup based in Austin"
(ZHawk's city), Excel incompetence (ZHawk's self-own) — and invented
fresh specifics under dispute ("fine, dad's fund", a "viet coworker's
Slack message"). Both dossiers were co-loaded in WHO'S TALKING; under
sustained-roast pressure the model blended them. Fix: receipts at
answer time — distinctive claims in an ungrounded BANTER answer
(tickers + mid-sentence capitalized entities) must appear in the
ASKER'S OWN material (their profile section, their chat lines, the
question); violations regenerate once, then strip.

Also: the CURRENT TIME header used to be PREPENDED to the system
instruction — a per-minute-changing prefix that defeated Gemini's
implicit caching for the entire static prompt behind it. It now goes
AFTER the static prompt.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


_KYLE_MATERIAL = """\
- **BK** (bankerkyle, <@423994649317736448>): works in M&A at a bank.
**Recent personal life.**
- Hiked Capitol Peak — "I'm gonna lose 3-5 toenails" + [the 14er saga]
- Plays Valorant during work — "I'm gonna be playing Valorant all week"
- Owes his dad money — "My dad isn't rescuing me. I owe him money"
BK (bankerkyle): Chips dumping
BK (bankerkyle): I officially declare qqq will close green today
"""


def test_cross_attributed_claims_flagged():
    from discord_bot.bot import _clapback_fidelity_violations
    answer = (
        "you're squatting at #8 while you stress over your overnight "
        "XSP 750 calls, alt-tabbing out of Valorant to answer your "
        "coworker's Slack message about a startup based in Austin."
    )
    v = _clapback_fidelity_violations(answer, _KYLE_MATERIAL)
    for tok in ("XSP", "Slack", "Austin"):
        assert tok in v, f"{tok} is not kyle's material — must flag: {v}"
    assert "Valorant" not in v, f"Valorant IS kyle's material: {v}"
    _ok("cross-attributed tickers + entities flagged; real receipts pass")


def test_clean_clapback_passes():
    from discord_bot.bot import _clapback_fidelity_violations
    answer = (
        "big talk from the guy grinding Valorant with four missing "
        "toenails from Capitol Peak. you owe your dad money, champ — "
        "that's the receipt that matters."
    )
    v = _clapback_fidelity_violations(answer, _KYLE_MATERIAL)
    assert v == [], f"clean clapback wrongly flagged: {v}"
    _ok("clapback built from the asker's own receipts passes clean")


def test_asker_material_scoped_to_asker():
    from discord_bot.bot import _asker_material_surface
    profiles = (
        "WHO'S TALKING:\n"
        "- **BK** (bankerkyle, <@1>): M&A banker, Capitol Peak toenails.\n"
        "- **ZHawk** (.zhawk, <@2>): XSP 745P trade, lives in Austin.\n"
    )
    chat = (
        "ZHawk (.zhawk): 14ers aren't hard\n"
        "BK (bankerkyle): I officially declare qqq will close green\n"
    )
    surface = _asker_material_surface(
        profiles, chat, "bankerkyle", "BK", "roast me"
    )
    low = surface.lower()
    assert "capitol peak" in low, "asker's own profile section missing"
    assert "xsp" not in low and "austin" not in low, (
        "co-loaded member's dossier leaked into the asker surface"
    )
    assert "qqq will close green" in low, "asker's chat lines missing"
    assert "14ers aren" not in low, "other members' chat lines leaked"
    _ok("material surface = asker's section + asker's lines only")


def test_time_header_no_longer_prefixes():
    from discord_bot.bot import (
        _build_runtime_system_instruction,
        _ASK_SYSTEM_INSTRUCTION,
    )
    built = _build_runtime_system_instruction()
    assert built.startswith(_ASK_SYSTEM_INSTRUCTION[:400]), (
        "static prompt must be the cacheable PREFIX — a timestamp "
        "prefix defeats Gemini implicit caching every minute"
    )
    assert "CURRENT TIME (UTC)" in built, "time header must survive"
    assert built.index("CURRENT TIME (UTC)") > len(built) // 2, (
        "time header must sit in the dynamic suffix"
    )
    _ok("system instruction: static cacheable prefix, dynamic time suffix")


if __name__ == "__main__":
    print("=== clapback fidelity + cache prefix smoke ===")
    test_cross_attributed_claims_flagged()
    test_clean_clapback_passes()
    test_asker_material_scoped_to_asker()
    test_time_header_no_longer_prefixes()
    print("\nALL CLAPBACK FIDELITY SMOKE TESTS PASS")
