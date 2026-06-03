"""End-to-end verification that /ask actually returns the correct AH
quote for stocks asked after 4 PM ET.

What this exercises:
  1. Production system instruction (ghost-writer reframe)
  2. Production tool list (Google Search + 4 function declarations)
  3. Real lookup_market_price executor (which now routes through Yahoo
     when AFTER-HOURS / PRE-MARKET, falls back to Finnhub)
  4. Real Gemini call with tool-use roundtrip
  5. The final prose answer

Asks: "what's TSLA and GTLB doing afterhours"

Pass criteria:
  - Gemini chose lookup_market_price (not Google Search) for this Q
  - Tool response contained data_freshness=live_extended_hours
  - Final prose mentions both regular close AND AH price for at least
    one of the symbols (i.e. correctly broke out the AH move)

Run: PYTHONPATH=. py.exe scripts/_oneoff_ah_quote_e2e_test.py
"""

import asyncio
import json
import os
import sys

from dotenv import load_dotenv

load_dotenv()


async def main():
    from google.genai import Client, types
    from discord_bot.bot import (
        _ASK_SYSTEM_INSTRUCTION,
        _build_chat_search_tool,
        _build_user_profile_tool,
        _build_trade_log_tool,
        _build_market_price_tool,
        _execute_chat_search,
        _execute_user_profile,
        _execute_trade_log,
        _execute_market_price,
    )

    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        print("FAIL: GOOGLE_API_KEY not set"); sys.exit(1)
    client = Client(api_key=api_key)

    question = "what's TSLA and GTLB doing afterhours"

    tools = [
        types.Tool(google_search=types.GoogleSearch()),
        _build_chat_search_tool(),
        _build_user_profile_tool(),
        _build_trade_log_tool(),
        _build_market_price_tool(),
    ]
    tool_config = types.ToolConfig(include_server_side_tool_invocations=True)
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
        tool_config=tool_config,
    )

    executors = {
        "search_chat_messages": _execute_chat_search,
        "lookup_user_profile":  _execute_user_profile,
        "lookup_trade_log":     _execute_trade_log,
        "lookup_market_price":  _execute_market_price,
    }

    # Multi-turn tool-call loop. Mirrors what the production /ask does:
    # keep generating, dispatch any function_calls, feed responses back,
    # until the model returns prose with no further tool calls.
    history = [types.Content(role="user", parts=[types.Part.from_text(text=question)])]
    tool_log: list[dict] = []
    final_text = ""

    for turn in range(6):
        resp = await client.aio.models.generate_content(
            model="gemini-flash-lite-latest",
            contents=history,
            config=config,
        )

        # Echo back the model's WHOLE response content (preserves
        # thought_signature attached to function_call parts — required
        # by the Gemini API since 2025).
        candidate = resp.candidates[0]
        model_parts = list(candidate.content.parts or [])

        fn_calls = [(i, p.function_call) for i, p in enumerate(model_parts)
                    if getattr(p, "function_call", None)]
        text_parts = [p.text for p in model_parts if getattr(p, "text", None)]

        if fn_calls:
            history.append(types.Content(role="model", parts=model_parts))
            response_parts = []
            for _, fc in fn_calls:
                exec_fn = executors.get(fc.name)
                if not exec_fn:
                    tool_resp = {"error": f"unknown tool {fc.name}"}
                else:
                    args = dict(fc.args or {})
                    print(f"  turn {turn}: calling {fc.name}({args})")
                    tool_resp = await exec_fn(args)
                tool_log.append({"tool": fc.name, "args": dict(fc.args or {}), "resp": tool_resp})
                response_parts.append(
                    types.Part.from_function_response(
                        name=fc.name, response={"result": tool_resp}
                    )
                )
            history.append(types.Content(role="user", parts=response_parts))
            continue

        # No more function calls -> final prose
        final_text = "".join(text_parts).strip()
        break

    print()
    print(f"Question: {question!r}")
    print()
    print("Tools called:")
    for entry in tool_log:
        if entry["tool"] == "lookup_market_price":
            symbols = entry["resp"].get("quotes") or []
            print(f"  {entry['tool']}({entry['args']})")
            for q in symbols:
                print(f"    {q.get('symbol')}: source={q.get('source')} "
                      f"price={q.get('price')} freshness={q.get('data_freshness')} "
                      f"ah_move={q.get('extended_hours_change_pct')}")
        else:
            print(f"  {entry['tool']}({entry['args']})")
    print()
    print(f"Final prose (length {len(final_text)}):")
    print(final_text)
    print()

    # Pass criteria
    checks = []
    chose_price_tool = any(e["tool"] == "lookup_market_price" for e in tool_log)
    checks.append(("chose lookup_market_price", chose_price_tool))

    has_live_ah = False
    for entry in tool_log:
        if entry["tool"] == "lookup_market_price":
            for q in (entry["resp"].get("quotes") or []):
                if q.get("data_freshness") == "live_extended_hours":
                    has_live_ah = True; break
    checks.append(("tool response has live_extended_hours", has_live_ah))

    # Prose mentions both regular close AND AH print for at least one symbol
    prose_low = final_text.lower()
    mentions_close = any(s in prose_low for s in ("closed", "close", "regular session", "4 pm"))
    mentions_ah = any(s in prose_low for s in ("after-hour", "afterhour", "after hour", "ah ", "post-market", "post market"))
    checks.append(("prose mentions close (regular session)", mentions_close))
    checks.append(("prose mentions AH/post-market context", mentions_ah))

    print("Checks:")
    all_pass = True
    for label, ok in checks:
        mark = "PASS" if ok else "FAIL"
        if not ok: all_pass = False
        print(f"  {mark} {label}")
    print()
    print("E2E VERDICT:", "PASS" if all_pass else "FAIL")
    sys.exit(0 if all_pass else 2)


asyncio.run(main())
