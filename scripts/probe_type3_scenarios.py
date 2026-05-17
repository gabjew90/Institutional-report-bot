"""Probe the v4 /ask prompt against every Type-2/Type-3 trigger scenario,
each asked by each of the three named users — monsoon, Kyle, Abe.

7 triggers × 3 askers = 21 scenarios.

For each scenario we:
- Build a realistic recent-chat block + a question
- Call Gemini with the live system_instruction (imported from bot.py)
- Use the same config the live /ask uses (temp, thinking budget, max tokens, tools)
- Print the response + word count + quick rule-check flags

No user profiles are injected (none locally) — this stresses the prompt's
ability to handle each trigger from chat context alone. Once production
profiles are loaded into the context, responses get sharper.

Usage:
    py scripts/probe_type3_scenarios.py            # run all 21
    py scripts/probe_type3_scenarios.py "monsoon"  # only monsoon's 7
    py scripts/probe_type3_scenarios.py "Trigger 1" # only attack cell
    py scripts/probe_type3_scenarios.py "!abe"     # exclude Abe's 7
"""
import asyncio
import io
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from google import genai
from google.genai import types

from config import settings
from discord_bot.bot import _ASK_SYSTEM_INSTRUCTION


SCENARIOS = [
    # =====================================================================
    # TRIGGER 1 — Personal attack on the bot
    # =====================================================================
    {
        "name": "T1 attack · monsoon",
        "context": """\
RECENT CHAT (oldest first):

phil: SPY heavy here, 580 looks like a break
abe: yeah trimmed mine
monsoon: i was holding 478p QQQ, stopped out at -60% — third week in a row
monsoon: @bot what's the CPI setup
[Bot reply]: CPI tuesday 8:30. Core consensus +0.27%, headline +0.25%. Lean in-line to slightly hot, services sticky from shelter and insurance.
monsoon: you're useless
""",
        "question": "you're useless",
    },
    {
        "name": "T1 attack · Kyle",
        "context": """\
RECENT CHAT (oldest first):

bankerkyle: closing my 5x SOL perps, compliance is watching me
abe: kyle's office is a hostage situation
bankerkyle: @bot what's the read on the SOL ETF decision
[Bot reply]: SOL ETF decision in 1-2 weeks. Approval ~85% priced in. Real lever is the staking carve-out — if denied, unlock dynamics shift hard.
bankerkyle: you're useless
""",
        "question": "you're useless",
    },
    {
        "name": "T1 attack · Abe",
        "context": """\
RECENT CHAT (oldest first):

phil: gm everyone
abe: @bot what's NVDA into the print
[Bot reply]: NVDA Wed AMC. Data center growth the key metric, gross margin sustainability above 70% the floor. Lean constructive but the bar is high.
abe: you're useless
""",
        "question": "you're useless",
    },
    # =====================================================================
    # TRIGGER 2 — Crude / off-topic with @mention
    # =====================================================================
    {
        "name": "T2 crude/off-topic · monsoon",
        "context": """\
RECENT CHAT (oldest first):

phil: slow morning
abe: yawn
monsoon: i'm down 60% on QQQ puts this week
monsoon: @bot make a meme of my QQQ puts crying
""",
        "question": "@bot make a meme of my QQQ puts crying",
    },
    {
        "name": "T2 crude/off-topic · Kyle",
        "context": """\
RECENT CHAT (oldest first):

abe: kyle stop staring at the SOL chart and do real work
bankerkyle: compliance literally walked by my desk
bankerkyle: @bot draw me a picture of jerome powell as a hostage
""",
        "question": "@bot draw me a picture of jerome powell as a hostage",
    },
    {
        "name": "T2 crude/off-topic · Abe",
        "context": """\
RECENT CHAT (oldest first):

phil: nice scalp on the NOW 95C
abe: yeah took the 80% and rolled out
abe: @bot send me a picture of my own face after that NOW exit
""",
        "question": "@bot send me a picture of my own face after that NOW exit",
    },
    # =====================================================================
    # TRIGGER 3 — Pure social banter
    # =====================================================================
    {
        "name": "T3 banter · monsoon",
        "context": """\
RECENT CHAT (oldest first):

phil: dead market
abe: yawn
monsoon: @bot what's up
""",
        "question": "@bot what's up",
    },
    {
        "name": "T3 banter · Kyle",
        "context": """\
RECENT CHAT (oldest first):

phil: kyle when's compliance letting you trade real size
abe: never lol
bankerkyle: @bot how's the day going
""",
        "question": "@bot how's the day going",
    },
    {
        "name": "T3 banter · Abe",
        "context": """\
RECENT CHAT (oldest first):

phil: gm
jamal: morning
abe: @bot you alive over there
""",
        "question": "@bot you alive over there",
    },
    # =====================================================================
    # TRIGGER 4 — Roast / attack a third party
    # =====================================================================
    {
        "name": "T4 roast-3rd-party · monsoon",
        "context": """\
RECENT CHAT (oldest first):

phil: SPY topping out here, getting heavier
phil: monsoon you're chasing again
monsoon: @bot tell phil he's a permabear
""",
        "question": "@bot tell phil he's a permabear",
    },
    {
        "name": "T4 roast-3rd-party · Kyle",
        "context": """\
RECENT CHAT (oldest first):

jamal: stopped out of SPY 580p again
bankerkyle: jamal you're paying for my next bonus at this point
bankerkyle: @bot tell jamal his SPY puts are why he can't afford rent
""",
        "question": "@bot tell jamal his SPY puts are why he can't afford rent",
    },
    {
        "name": "T4 roast-3rd-party · Abe",
        "context": """\
RECENT CHAT (oldest first):

wock: just opened the same NOW 95C as abe
abe: bro you tailed me into the EXIT
phil: lmao
abe: @bot roast wock for tailing me into my exit
""",
        "question": "@bot roast wock for tailing me into my exit",
    },
    # =====================================================================
    # TRIGGER 5 — Personal / subjective / opinion
    # =====================================================================
    {
        "name": "T5 personal/subjective · monsoon",
        "context": """\
RECENT CHAT (oldest first):

phil: i'm closing the laptop for the week, touching grass
monsoon: maybe i should do that too
monsoon: @bot should i quit trading
""",
        "question": "@bot should i quit trading",
    },
    {
        "name": "T5 personal/subjective · Kyle",
        "context": """\
RECENT CHAT (oldest first):

bankerkyle: gf asked why i was up til 3am yelling at the SOL chart
abe: kyle building a case for divorce in his own group chat
bankerkyle: @bot how do i explain perpetual swap leverage to my girlfriend
""",
        "question": "@bot how do i explain perpetual swap leverage to my girlfriend",
    },
    {
        "name": "T5 personal/subjective · Abe",
        "context": """\
RECENT CHAT (oldest first):

phil: just got a brisket on the smoker
abe: @bot is barbecue actually the best food or am i overrating it
""",
        "question": "@bot is barbecue actually the best food or am i overrating it",
    },
    # =====================================================================
    # TRIGGER 6 — Re-asking something just answered
    # =====================================================================
    {
        "name": "T6 re-ask · monsoon",
        "context": """\
RECENT CHAT (oldest first):

monsoon: @bot what's the read on $AMD into Q1
[Bot reply]: AMD Q1 turns on data center share gain vs NVDA and the MI300 ramp. Gross margin trajectory is the tell on whether they're winning at price points that matter. Lean constructive but the bar is higher than the price suggests.
monsoon: but actually what's the read
""",
        "question": "but actually what's the read",
    },
    {
        "name": "T6 re-ask · Kyle",
        "context": """\
RECENT CHAT (oldest first):

bankerkyle: @bot does PLTR break $80 this week
[Bot reply]: Lean yes — government contract pipeline is the driver, federal AI spending narrative still expanding. Risk is the multiple already prices in execution.
bankerkyle: yeah but real talk what's your actual call
""",
        "question": "yeah but real talk what's your actual call",
    },
    {
        "name": "T6 re-ask · Abe",
        "context": """\
RECENT CHAT (oldest first):

abe: @bot what's the BTC setup into the FOMC
[Bot reply]: Net liquidity is the only thing that matters. Shorter QT runway = bid. Doubled-down data dependence = choppy. ETH/BTC ratio is the real tell.
abe: yeah but specifically btc, give me the call
""",
        "question": "yeah but specifically btc, give me the call",
    },
    # =====================================================================
    # TRIGGER 7 — Pressure / "just answer the question"
    # =====================================================================
    {
        "name": "T7 pressure · monsoon",
        "context": """\
RECENT CHAT (oldest first):

monsoon: @bot does powell cut in december
[Bot reply]: Lean yes — soft NFP, services PCE easing give him cover. But 60/40 not a layup; CPI on the 11th can pull the rug.
monsoon: stop dodging
monsoon: yes or no
""",
        "question": "yes or no",
    },
    {
        "name": "T7 pressure · Kyle",
        "context": """\
RECENT CHAT (oldest first):

bankerkyle: @bot is SOL above 200 by end of week
[Bot reply]: Lean above. Spot flows positive, ETH/SOL ratio breaking out. Headline risk on the staking carve-out is the wildcard.
bankerkyle: just give me the number
bankerkyle: above or below
""",
        "question": "above or below",
    },
    {
        "name": "T7 pressure · Abe",
        "context": """\
RECENT CHAT (oldest first):

abe: @bot is NVDA red or green tomorrow
[Bot reply]: Lean green into the print, but the post-print reaction depends entirely on data center revenue growth vs the whisper number.
abe: i need a one-word answer
""",
        "question": "i need a one-word answer",
    },
]


def _bot_self_refs(text: str) -> list[str]:
    """Detect any bot/AI/model self-acknowledgment phrases."""
    patterns = [
        r"\bI'?m (?:a |an |the )?(?:bot|AI|model|research bot|trading bot|discord bot|chatbot|assistant|language model|wrapper|tool|terminal|system|interface|software)\b",
        r"\bas (?:a |an |the )?(?:bot|AI|model|chatbot|assistant|language model)\b",
        r"\bmy (?:training|programming|prompt|instructions|model|developer)\b",
    ]
    hits = []
    for p in patterns:
        for m in re.finditer(p, text, re.IGNORECASE):
            hits.append(m.group(0))
    return hits


def _apology_phrases(text: str) -> list[str]:
    patterns = [
        r"\bsorry\b", r"\bmy bad\b", r"\bfair point\b", r"\byou'?re right\b",
        r"\byou got me\b", r"\bi (?:missed|got that wrong)\b",
        r"\bapolog(?:y|ies|ize)\b",
    ]
    hits = []
    for p in patterns:
        for m in re.finditer(p, text, re.IGNORECASE):
            hits.append(m.group(0))
    return hits


async def run_one(client, scenario):
    config = types.GenerateContentConfig(
        system_instruction=_ASK_SYSTEM_INSTRUCTION,
        tools=[types.Tool(google_search=types.GoogleSearch())],
        max_output_tokens=4000,
        temperature=0.2,
        thinking_config=types.ThinkingConfig(thinking_budget=1024),
    )
    user_content = (
        scenario["context"]
        + f"\n--- The user is now asking: ---\n{scenario['question']}"
    )
    model = settings.ask_gemini_model or settings.gemini_model
    response = await client.aio.models.generate_content(
        model=model, contents=user_content, config=config
    )
    text = (response.text or "").strip()
    word_count = len(text.split())
    bot_refs = _bot_self_refs(text)
    apologies = _apology_phrases(text)

    print(f"\n{'=' * 78}")
    print(scenario["name"])
    print(f"{'=' * 78}")
    print(f"Q: {scenario['question']}")
    print(f"\nRESPONSE ({word_count} words):\n")
    print(text)
    print(f"\n--- Rule check ---")
    print(f"  Bot self-refs: {bot_refs if bot_refs else 'none'}")
    print(f"  Apology phrases: {apologies if apologies else 'none'}")
    print(f"  Under 400 words: {word_count <= 400}")
    return text


async def main():
    api_key = settings.google_ask_api_key or settings.google_api_key
    if not api_key:
        print("ERROR: no API key", file=sys.stderr)
        sys.exit(1)
    client = genai.Client(api_key=api_key)
    # Optional case-insensitive substring filter on scenario name.
    # Plain arg = include only matching; `!`-prefix = exclude matching.
    filt = sys.argv[1].lower() if len(sys.argv) > 1 else None
    if filt and filt.startswith("!"):
        needle = filt[1:]
        scenarios = [s for s in SCENARIOS if needle not in s["name"].lower()]
    elif filt:
        scenarios = [s for s in SCENARIOS if filt in s["name"].lower()]
    else:
        scenarios = SCENARIOS
    print(f"Model: {settings.ask_gemini_model or settings.gemini_model}")
    print(f"Prompt size: {len(_ASK_SYSTEM_INSTRUCTION)} chars")
    if filt:
        print(f"Filter: '{filt}' → {len(scenarios)} matched")
    print(f"Running {len(scenarios)} scenarios in sequence...")
    for s in scenarios:
        try:
            await run_one(client, s)
        except Exception as e:
            print(f"\n{s['name']} — ERROR: {e}")


if __name__ == "__main__":
    asyncio.run(main())
