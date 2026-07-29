"""Smoke: native code-execution wiring for /ask (2026-07-29).

The model can now write + run Python in Google's sandbox (verified
coexisting with the 8 function tools + Google Search on 3.5-flash-lite)
to answer analytical questions — payoff breakevens, monte carlo, IV
math, stats — and return rendered matplotlib charts. Structural safety:
the sandbox is Google's, so member code never touches Railway; the
model's composed TEXT answer still flows through every existing
disclosure/fidelity guard (raw code stdout is never posted); only
generated chart images are surfaced.

Covers:
  - code_execution tool present in the main /ask config path
  - image extraction pulls inline chart bytes, caps the count, ignores
    non-image inline data
  - the send-result normalizer accepts both a bare Embed and an
    (embed, files) tuple
  - the prompt tells the model the capability exists
"""

import inspect
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _img_part(data, mime="image/png"):
    return SimpleNamespace(
        inline_data=SimpleNamespace(data=data, mime_type=mime),
        text=None, executable_code=None, code_execution_result=None,
        function_call=None,
    )


def _resp(parts):
    return SimpleNamespace(
        candidates=[SimpleNamespace(content=SimpleNamespace(parts=parts))]
    )


def test_code_execution_tool_wired():
    import discord_bot.bot as bot
    src = inspect.getsource(bot._answer_with_gemini)
    head = src.split("Tool-calling loop", 1)[0]
    assert "ToolCodeExecution()" in head, (
        "code_execution tool must be in the main /ask config"
    )
    _ok("code_execution tool wired into the main /ask config")


def test_image_extraction_final_only_and_filters():
    from discord_bot.bot import _extract_code_images
    parts = [
        _img_part(b"DRAFT1"),
        _img_part(b"NOTIMG", mime="text/plain"),  # ignored
        _img_part(b"DRAFT2"),
        _img_part(b"FINAL"),
    ]
    imgs = _extract_code_images(_resp(parts))
    assert all(m.startswith("image/") for _b, m in imgs), imgs
    # only the FINAL render surfaces — iterated drafts collapse away
    assert len(imgs) == 1 and imgs[0][0] == b"FINAL", imgs
    _ok("chart-image extraction filters non-images, keeps final render")


def test_no_images_returns_empty():
    from discord_bot.bot import _extract_code_images
    parts = [SimpleNamespace(inline_data=None, text="just text",
                             executable_code=None,
                             code_execution_result=None, function_call=None)]
    assert _extract_code_images(_resp(parts)) == []
    _ok("text-only response yields no images")


def test_result_normalizer():
    import discord_bot.bot as bot
    import discord
    e = discord.Embed(description="x")
    emb, files = bot._normalize_ask_result(e)
    assert emb is e and files == [], "bare embed must normalize to (e, [])"
    emb2, files2 = bot._normalize_ask_result((e, ["f1", "f2"]))
    assert emb2 is e and files2 == ["f1", "f2"], "tuple must pass through"
    _ok("send-result normalizer handles embed and (embed, files)")


def test_prompt_announces_capability():
    from discord_bot.bot import _ASK_SYSTEM_INSTRUCTION as S
    assert "run" in S.lower() and "code" in S.lower() and (
        "compute" in S.lower() or "python" in S.lower()), (
        "prompt must tell the model the code-execution capability exists"
    )
    _ok("prompt announces the code-execution capability")


if __name__ == "__main__":
    print("=== code execution wiring smoke ===")
    test_code_execution_tool_wired()
    test_image_extraction_final_only_and_filters()
    test_no_images_returns_empty()
    test_result_normalizer()
    test_prompt_announces_capability()
    print("\nALL CODE EXECUTION SMOKE TESTS PASS")
