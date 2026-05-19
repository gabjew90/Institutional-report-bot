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

# Build a small synthetic message block (50 lines).
msgs = [
    {
        "timestamp": "2026-05-12T14:30:00",
        "content": f"i'm just gonna sit on my hands today, market's gonna chop",
        "embed_texts": [],
        "image_count": 0,
    }
    for _ in range(50)
]
msgs_block = _format_messages_block(msgs)

prompt = PROFILE_PROMPT.format(
    display_name="BK",
    username="bankerkyle",
    user_id=423994649317736448,
    msg_count=50,
    messages_block=msgs_block,
    today_utc="2026-05-19",
)

print(f"=== prompt size: {len(prompt)} chars ===")
print()


def run_test(label, **extra_config):
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
    print(f"text length: {len(text)}")
    print(f"text (first 800 chars): {text[:800]!r}")
    try:
        cand = resp.candidates[0]
        print(f"finish_reason: {cand.finish_reason}")
    except Exception as e:
        print(f"(no candidate: {e})")
    print()


# Test 1: no schema
run_test("NO SCHEMA")

# Test 2: with schema
run_test("WITH SCHEMA", response_schema=_build_schema())
