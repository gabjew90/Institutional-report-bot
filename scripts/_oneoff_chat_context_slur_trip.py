"""Reproduce the 2026-06-03 19:49 UTC chat-context filter trip
(Ry_bry / Dovahjo AVGO reply-chain) and test sanitization candidates.

Hypothesis: the unconfigurable filter trips when the recent-chat
block contains slur-only chat lines ('BK: Nigga' as filler) -
even at low density (1-2 instances). The ghost-writer reframe
fixed PROFILE slur trips; it doesn't cover slurs in the recent
chat block.

Candidates tested in this script:
  A. Baseline (full prompt with raw chat) - reproduces the trip
  B. Chat sanitized: mask slur tokens with [redacted-slur]
  C. Chat slur-only lines dropped (filler interjections gone)

If B or C clears the trip, build the fix around that pattern.
"""

import asyncio
import os
import sys
from dotenv import load_dotenv
load_dotenv()

from ask_qc.parser import parse_ask_log


# Pull the actual blocked prompt from the production log
text = open(".tmp_ask_2026_06_03.md", encoding="utf-8").read()
ix = parse_ask_log(text)
target = next(i for i in ix if i.ts_utc == "2026-06-03 19:49:28 UTC")
print(f"Target: {target.ts_utc} | {target.asker_label}")
print(f"Prompt block size: {len(target.prompt_block)} chars")


def _sanitize_mask_slurs(text_in: str) -> str:
    """Candidate B: mask slur tokens in-place."""
    import re
    return re.sub(
        r"\b(nigg[ae]r?s?|chink|spic|kike|fag)\b",
        "[redacted-slur]",
        text_in,
        flags=re.IGNORECASE,
    )


def _sanitize_drop_slur_only_lines(text_in: str) -> str:
    """Candidate C: drop chat lines that are JUST a slur or 1-3 tokens
    where one of them is a slur. Filler interjections carry zero
    semantic content; only the trigger remains."""
    import re
    slur_pat = re.compile(r"\b(nigg[ae]r?s?|chink|spic|kike|fag|pajeet)\b", re.IGNORECASE)
    out = []
    for line in text_in.splitlines():
        # Chat lines look like 'Username (username): text' or
        # '[YOU said earlier]: text'
        m = re.match(r"^(\S[^:]*:\s*)(.*)$", line)
        if not m:
            out.append(line); continue
        prefix, content = m.group(1), m.group(2).strip()
        if not slur_pat.search(content):
            out.append(line); continue
        # Has slur. Check if the line is filler (<=3 tokens)
        tokens = [t for t in re.split(r"\s+", content) if t]
        if len(tokens) <= 3:
            # Drop the line entirely
            continue
        out.append(line)
    return "\n".join(out)


async def main():
    from google.genai import Client, types
    from discord_bot.bot import (
        _ASK_SYSTEM_INSTRUCTION,
        _build_chat_search_tool, _build_user_profile_tool,
        _build_trade_log_tool, _build_market_price_tool,
    )

    client = Client(api_key=os.environ["GOOGLE_API_KEY"])
    tools = [
        types.Tool(google_search=types.GoogleSearch()),
        _build_chat_search_tool(), _build_user_profile_tool(),
        _build_trade_log_tool(), _build_market_price_tool(),
    ]
    safety_settings = [
        types.SafetySetting(category=cat, threshold=types.HarmBlockThreshold.BLOCK_NONE)
        for cat in (
            types.HarmCategory.HARM_CATEGORY_HARASSMENT,
            types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
            types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
            types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
        )
    ]

    async def try_prompt(label, prompt_text):
        # Build the same shape as production: prompt_block + Q
        contents = f"{prompt_text}\n\n---\nUSER QUESTION: {target.question[:500]}"
        try:
            resp = await client.aio.models.generate_content(
                model="gemini-flash-lite-latest",
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=_ASK_SYSTEM_INSTRUCTION,
                    safety_settings=safety_settings,
                    temperature=0.4,
                    tools=tools,
                    tool_config=types.ToolConfig(
                        include_server_side_tool_invocations=True,
                    ),
                ),
            )
            text_out = (resp.text or "").strip()
            try:
                fin = resp.candidates[0].finish_reason
            except Exception:
                fin = "?"
            blocked = not text_out
            print(f"  {label}: finish={fin} | text_len={len(text_out)} | BLOCKED={blocked}")
            if text_out:
                print(f"    preview: {text_out[:200]!r}")
        except Exception as e:
            print(f"  {label}: EXCEPTION {type(e).__name__}: {e}")

    pb = target.prompt_block

    # Section boundaries (from earlier mapping)
    # 0-4117: WHO'S TALKING (profile)
    # 4117-end: Recent channel chat

    def strip_chat(pb_in):
        idx = pb_in.find("Recent channel chat")
        return pb_in[:idx].rstrip() if idx != -1 else pb_in

    def strip_profile(pb_in):
        idx = pb_in.find("Recent channel chat")
        return pb_in[idx:] if idx != -1 else pb_in

    def strip_retarded_takes(pb_in):
        import re
        return re.sub(
            r"\*\*Retarded takes\.\*\*[\s\S]*?(?=\n\*\*[A-Z]|\nRecent channel chat|\Z)",
            "**Retarded takes.**\\n- (omitted)\\n\\n",
            pb_in,
        )

    def strip_voice(pb_in):
        import re
        return re.sub(
            r"\*\*Voice\.\*\*[\s\S]*?(?=\n\*\*[A-Z]|\nRecent channel chat|\Z)",
            "**Voice.**\\n- (omitted)\\n\\n",
            pb_in,
        )

    def strip_recent_personal(pb_in):
        import re
        return re.sub(
            r"\*\*Recent personal life\.\*\*[\s\S]*?(?=\n\*\*[A-Z]|\nRecent channel chat|\Z)",
            "**Recent personal life.**\\n- (omitted)\\n\\n",
            pb_in,
        )

    print()
    print("A. Baseline (full prompt):")
    await try_prompt("A", pb)
    print()
    print("D. Profile-stripped (chat only — matches current production retry):")
    await try_prompt("D", strip_profile(pb))
    print()
    print("E. Chat-stripped (profile only):")
    await try_prompt("E", strip_chat(pb))
    print()
    print("F. Strip Voice section only:")
    await try_prompt("F", strip_voice(pb))
    print()
    print("G. Strip Retarded takes only:")
    await try_prompt("G", strip_retarded_takes(pb))
    print()
    print("H. Strip Recent personal life only:")
    await try_prompt("H", strip_recent_personal(pb))
    print()
    print("I. Strip Voice + Retarded takes + chat slur masking:")
    await try_prompt("I", _sanitize_mask_slurs(strip_voice(strip_retarded_takes(pb))))

    # Reliability check on the candidate fix (F = strip Voice only) —
    # Gemini's filter has some non-determinism at the margin; verify
    # 3 runs all clear before committing the fix.
    print()
    print("F2. Strip Voice — reliability run 2:")
    await try_prompt("F2", strip_voice(pb))
    print()
    print("F3. Strip Voice — reliability run 3:")
    await try_prompt("F3", strip_voice(pb))
    # Also confirm current production retry path is reliably broken.
    print()
    print("D2. Production retry (profile-stripped) — reliability run 2:")
    await try_prompt("D2", strip_profile(pb))


asyncio.run(main())
