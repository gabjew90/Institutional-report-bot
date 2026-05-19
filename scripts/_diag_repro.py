"""One-shot diagnostic: replay the failing backfill call with the
EXACT prompt + config and dump everything about the response.

Pulls a real user's prompt structure but uses synthetic minimal
messages to keep it fast. We're not testing content here, we're
testing whether the SCHEMA + PROMPT-LENGTH combination is the cause."""
import os
import sys

sys.path.insert(0, "/app")

from google import genai
from google.genai import types

from scripts.backfill_user_profiles import (
    PROFILE_PROMPT,
    _format_messages_block,
)


def _build_schema():
    return types.Schema(
        type=types.Type.OBJECT,
        properties={
            "profile_text": types.Schema(type=types.Type.STRING),
            "trader_score": types.Schema(type=types.Type.INTEGER),
            "trader_rationale": types.Schema(type=types.Type.STRING),
            "racial_humor_score": types.Schema(type=types.Type.INTEGER),
        },
        required=[
            "profile_text",
            "trader_score",
            "trader_rationale",
            "racial_humor_score",
        ],
    )


client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])

def make_prompt(n_msgs: int, content_variant: str = "clean"):
    if content_variant == "clean":
        msgs = [
            {
                "timestamp": "2026-05-12T14:30:00",
                "content": "i'm just gonna sit on my hands today, market's gonna chop",
                "embed_texts": [],
                "image_count": 0,
            }
            for _ in range(n_msgs)
        ]
    elif content_variant == "edgy":
        # Realistic banter — slurs / coarse content like the actual chat
        snippets = [
            "fuck it we ball, sized up on $NVDA",
            "should've listened to @abe, this is a chop",
            "wtf is wrong with you retards buying tops",
            "i hate this market, dog shit price action",
            "calls printing baby, screen shot incoming",
            "your honor my client was buying puts",
            "lmaoooo terlin's accent killing me",
            "stop bag holding $WEN, take the L",
            "based and SPYpilled, full port lottos",
            "$SPX 6450 here we go, calls",
        ]
        msgs = [
            {
                "timestamp": "2026-05-12T14:30:00",
                "content": snippets[i % len(snippets)],
                "embed_texts": [],
                "image_count": 0,
            }
            for i in range(n_msgs)
        ]
    msgs_block = _format_messages_block(msgs)
    return PROFILE_PROMPT.format(
        display_name="BK",
        username="bankerkyle",
        user_id=423994649317736448,
        msg_count=n_msgs,
        messages_block=msgs_block,
        today_utc="2026-05-19",
    )


def run_test(label, prompt, **extra_config):
    print(f"--- {label} ---")
    config_kwargs = dict(
        temperature=0.3,
        max_output_tokens=2500,
        response_mime_type="application/json",
    )
    config_kwargs.update(extra_config)
    resp = client.models.generate_content(
        model="gemini-3.1-flash-lite-preview",
        contents=prompt,
        config=types.GenerateContentConfig(**config_kwargs),
    )
    text = (resp.text or "").strip()
    print(f"prompt length: {len(prompt)} chars / text length: {len(text)}")
    print(f"text (first 600): {text[:600]!r}")
    try:
        cand = resp.candidates[0]
        print(f"finish_reason: {cand.finish_reason}")
    except Exception as e:
        print(f"(no candidate: {e})")
    print()


schema = _build_schema()

# Test grid: scale message count + content variant + schema
print("=== TEST GRID ===\n")
run_test("50 clean, NO schema",     make_prompt(50, "clean"))
run_test("50 clean, SCHEMA",        make_prompt(50, "clean"),  response_schema=schema)
run_test("500 clean, SCHEMA",       make_prompt(500, "clean"), response_schema=schema)
run_test("3000 clean, SCHEMA",      make_prompt(3000, "clean"), response_schema=schema)
run_test("50 edgy, SCHEMA",         make_prompt(50, "edgy"),   response_schema=schema)
run_test("500 edgy, SCHEMA",        make_prompt(500, "edgy"),  response_schema=schema)
run_test("3000 edgy, SCHEMA",       make_prompt(3000, "edgy"), response_schema=schema)
