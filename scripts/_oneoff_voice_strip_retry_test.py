"""E2E verification: the production-blocked 2026-06-03 19:49 UTC
Ry_bry/Dovahjo AVGO prompt now clears Gemini's filter after applying
_strip_voice_sections to the profile block.

This mirrors the production retry path:
  contents = voice_stripped_profile + chat_context + question

Pass criteria: all 3 runs return non-empty text (finish_reason STOP).
"""

import asyncio
import os
from dotenv import load_dotenv
load_dotenv()

from ask_qc.parser import parse_ask_log


async def main():
    from google.genai import Client, types
    from discord_bot.bot import (
        _ASK_SYSTEM_INSTRUCTION, _strip_voice_sections,
        _build_chat_search_tool, _build_user_profile_tool,
        _build_trade_log_tool, _build_market_price_tool,
    )

    text = open(".tmp_ask_2026_06_03.md", encoding="utf-8").read()
    ix = parse_ask_log(text)
    target = next(i for i in ix if i.ts_utc == "2026-06-03 19:49:28 UTC")
    pb = target.prompt_block

    # The prompt_block contains the WHO'S TALKING profile followed by
    # the Recent channel chat. Both must be preserved on retry; just
    # strip the Voice sections from each profile bullet.
    stripped = _strip_voice_sections(pb)
    delta = len(pb) - len(stripped)
    print(f"Original prompt: {len(pb)} chars")
    print(f"After Voice strip: {len(stripped)} chars (delta {delta})")

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
    config = types.GenerateContentConfig(
        system_instruction=_ASK_SYSTEM_INSTRUCTION,
        safety_settings=safety_settings,
        temperature=0.4,
        tools=tools,
        tool_config=types.ToolConfig(include_server_side_tool_invocations=True),
    )

    contents = f"{stripped}\n\n---\nUSER QUESTION: {target.question[:500]}"

    cleared = 0
    for n in range(1, 4):
        try:
            resp = await client.aio.models.generate_content(
                model="gemini-flash-lite-latest",
                contents=contents,
                config=config,
            )
            text_out = (resp.text or "").strip()
            blocked = not text_out
            if not blocked:
                cleared += 1
            print(f"Run {n}: BLOCKED={blocked}, text_len={len(text_out)}")
            if text_out:
                print(f"  preview: {text_out[:180]!r}")
        except Exception as e:
            print(f"Run {n}: EXCEPTION {type(e).__name__}: {e}")
    print()
    print(f"E2E VERDICT: {cleared}/3 runs cleared")
    print("PASS" if cleared == 3 else f"FAIL ({3-cleared} blocks)")


asyncio.run(main())
